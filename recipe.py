#!/usr/bin/env python

#   Copyright 2015 AlchemyAI
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.

import os, sys, string, time, re
import requests, json, urllib, urllib2, base64
import pymongo
from multiprocessing import Pool, Lock, Queue, Manager
import json

def main(search_term, num_questions):

    # Establish credentials for StackOverflow and AlchemyAPI
    credentials = get_credentials()
    
    # Pull Questions down from the StackExchange API
    raw_questions = search(search_term, num_questions, credentials)

    # De-duplicate Questions by ID
    unique_questions = dedup(raw_questions)

    # Enrich the body of the Questions using AlchemyAPI
    enriched_questions = enrich(credentials, unique_questions, sentiment_target = search_term)

    with open('stackoverflow.json', 'w') as outfile:
        json.dump(enriched_questions, outfile)

    # skipping this as putting data in json format on heroku    
    # # Store data in MongoDB
    # store(enriched_questions)
    #
    # # Print some interesting results to the screen
    # print_results()

    return

def get_credentials():
    creds = {}
    creds['request_key']    = str()
    creds['apikey']      = str()

    # If the file credentials.py exists, then grab values from it.
    # Values: "stack_overflow_request_key," "alchemy_apikey"
    # Otherwise, the values are entered by the user
    try:
        import credentials
        creds['request_key']    = credentials.stack_overflow_request_key
        creds['apikey']          = credentials.alchemy_apikey
    except:
        print "No credentials.py found"
        creds['request_key']    = raw_input("Enter your StackExchange request key: ")
        creds['apikey']          = raw_input("Enter your AlchemyAPI key: ")
        
    print "Using the following credentials:"
    print "\tStackExchange request key:", creds['request_key']
    print "\tAlchemyAPI key:", creds['apikey']

    # Test the validity of the AlchemyAPI key
    test_url = "http://access.alchemyapi.com/calls/info/GetAPIKeyInfo"
    test_parameters = {"apikey" : creds['apikey'], "outputMode" : "json"}
    test_results = requests.get(url=test_url, params=test_parameters)
    test_response = test_results.json()

    if 'OK' != test_response['status']:
        print "Oops! Invalid AlchemyAPI key (%s)" % creds['apikey']
        print "HTTP Status:", test_results.status_code, test_results.reason
        sys.exit()

    return creds


def search(search_term, num_questions, auth):
    # This collection will hold the Tweets as they are returned from Twitter
    collection = []
    # The search URL and headers
    url = "https://api.stackexchange.com/2.2/search/advanced?order=desc&sort=activity&site=stackoverflow&key=%s" % auth['request_key']

    max_count = 100
    page_number = 1
    has_more = ''
    # Can't stop, won't stop
    while True:
        print "Search iteration, item collection size: %d" % len(collection)
        count = min(max_count, int(num_questions)-len(collection))

        # Prepare the GET call
        if has_more:
            parameters = {
                'title' : search_term,
                'pagesize' : count,
                'page': page_number
                }

        else:
            parameters = {
                'title' : search_term,
                'pagesize' : count,
                }
            # get_url = url + '?' + urllib.urlencode(parameters)
        get_url = url + '&' + urllib.urlencode(parameters)


        # Make the GET call to Twitter
        results = requests.get(url=get_url)
        response = results.json()

        # Loop over statuses to store the relevant pieces of information
        for item in response['items']:
            title = item['title'].encode('utf-8')

            question = {}
            # Configure the fields you are interested in from the status object
            question['title']       = title
            question['question_id'] = item['question_id']
            question['time']        = item['creation_date']
            question['display_name'] = item['owner']['display_name'].encode('utf-8')
            
            collection    += [question]
        
            if len(collection) >= num_questions:
                print "Search complete! Found %d posts" % len(collection)
                return collection

        if response['has_more']:
            has_more = response['has_more']
            page_number += 1
        else:
            print "Uh-oh! StackOverflow has dried up. Only collected %d Questions (requested %d)" % (len(collection), num_questions)
            print "Last successful StackOverflow API call: %s" % get_url
            print "HTTP Status:", results.status_code, results.reason
            return collection

def enrich(credentials, questions, sentiment_target = ''):
    # Prepare to make multiple asynchronous calls to AlchemyAPI
    apikey = credentials['apikey']
    pool = Pool(processes = 10)
    mgr = Manager()
    result_queue = mgr.Queue()
    # Send each Tweet to the get_text_sentiment function
    for question in questions:
        pool.apply_async(get_text_sentiment, (apikey, question, sentiment_target, result_queue))

    pool.close()
    pool.join()
    collection = []
    while not result_queue.empty():
        collection += [result_queue.get()]
    
    print "Enrichment complete! Enriched %d Questions" % len(collection)
    return collection

def get_text_sentiment(apikey, question, target, output):

    # Base AlchemyAPI URL for targeted sentiment call
    alchemy_url = "http://access.alchemyapi.com/calls/text/TextGetTextSentiment"
    
    # Parameter list, containing the data to be enriched
    parameters = {
        "apikey" : apikey,
        "text"   : question['title'],
        "outputMode" : "json",
        "showSourceText" : 1
        }

    try:

        results = requests.get(url=alchemy_url, params=urllib.urlencode(parameters))
        response = results.json()

    except Exception as e:
        print "Error while calling TextGetTargetedSentiment on Question (ID %s)" % question['question_id']
        print "Error:", e
        return

    try:
        if 'OK' != response['status'] or 'docSentiment' not in response:
            print "Problem finding 'docSentiment' in HTTP response from AlchemyAPI"
            print response
            print "HTTP Status:", results.status_code, results.reason
            print "--"
            return

        question['sentiment'] = response['docSentiment']['type']
        question['score'] = 0.
        if question['sentiment'] in ('positive', 'negative'):
            question['score'] = float(response['docSentiment']['score'])
        output.put(question)

    except Exception as e:
        print "D'oh! There was an error enriching Question (ID %s)" % question['question_id']
        print "Error:", e
        print "Request:", results.url
        print "Response:", response

    return

def dedup(questions):
    used_ids = []
    collection = []
    for question in questions:
        if question['question_id'] not in used_ids:
            used_ids += [question['question_id']]
            collection += [question]
    print "After de-duplication, %d Questions" % len(collection)
    return collection

def store(questions):
    # Instantiate your MongoDB client
    mongo_client = pymongo.MongoClient()
    # Retrieve (or create, if it doesn't exist) the twitter_db database from Mongo
    db = mongo_client.stackoverflow_db
   
    db_questions = db.questions

    for question in questions:
        db_id = db_questions.insert(question)
 
    db_count = db_questions.count()
    
    print "Questions stored in MongoDB! Number of documents in twitter_db: %d" % db_count

    return

def print_results():

    print ''
    print ''
    print '###############'
    print '#    Stats    #'
    print '###############'
    print ''
    print ''    
    
    db = pymongo.MongoClient().stackoverflow_db
    questions = db.questions

    num_positive_questions = questions.find({"sentiment" : "positive"}).count()
    num_negative_questions = questions.find({"sentiment" : "negative"}).count()
    num_neutral_questions = questions.find({"sentiment" : "neutral"}).count()
    num_questions = questions.find().count()

    if num_questions != sum((num_positive_questions, num_negative_questions, num_neutral_questions)):
        print "Counting problem!"
        print "Number of questions (%d) doesn't add up (%d, %d, %d)" % (num_questions,
                                                                     num_positive_questions,
                                                                     num_negative_questions,
                                                                     num_neutral_questions)
        sys.exit()

    most_positive_question = questions.find_one({"sentiment" : "positive"}, sort=[("score", -1)])
    most_negative_question = questions.find_one({"sentiment" : "negative"}, sort=[("score", 1)])

    mean_results = questions.aggregate([{"$group" : {"_id": "$sentiment", "avgScore" : { "$avg" : "$score"}}}])

    avg_pos_score = mean_results['result'][2]['avgScore'] 
    avg_neg_score = mean_results['result'][1]['avgScore'] 
    
    print "SENTIMENT BREAKDOWN"
    print "Number (%%) of positive tweets: %d (%.2f%%)" % (num_positive_questions, 100*float(num_positive_questions) / num_questions)
    print "Number (%%) of negative tweets: %d (%.2f%%)" % (num_negative_questions, 100*float(num_negative_questions) / num_questions)
    print "Number (%%) of neutral tweets: %d (%.2f%%)" % (num_negative_questions, 100*float(num_negative_questions) / num_questions)
    print ""

    print "AVERAGE POSITIVE TWEET SCORE: %f" % float(avg_pos_score)
    print "AVERAGE NEGATIVE TWEET SCORE: %f" % float(avg_neg_score)
    print ""

    print "MOST POSITIVE TWEET"
    print "Text: %s" % most_positive_question['title']
    print "User: %s" % most_positive_question['display_name']
    print "Time: %s" % most_positive_question['time']
    print "Score: %f" % float(most_positive_question['score'])
    print ""

    print "MOST NEGATIVE TWEET"
    print "Text: %s" % most_negative_question['title']
    print "User: %s" % most_negative_question['display_name']
    print "Time: %s" % most_negative_question['time']
    print "Score: %f" % float(most_negative_question['score'])
    return

if __name__ == "__main__":

    if not len(sys.argv) == 3:
        print "ERROR: invalid number of command line arguments"
        print "SYNTAX: python recipe.py <SEARCH_TERM> <NUM_TWEETS>"
        print "\t<SEARCH_TERM> : the string to be used when searching for Questions"
        print "\t<NUM_TWEETS>  : the preferred number of Questions to pull from StackExchange's API"
        sys.exit()

    else:
        main(sys.argv[1], int(sys.argv[2]))
