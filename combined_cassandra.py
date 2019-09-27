#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Sep 24 13:55:37 2019

@author: JeffHalley
"""

from pyspark.sql import SparkSession
from pyspark.sql.functions import from_unixtime, col, window, broadcast, lower, explode, split, col, avg, sum, lag, to_date
from pyspark.sql.window import Window
from cassandra.cluster import Cluster


#create cassandra cluster
cluster = Cluster(["10.0.0.4" , "10.0.0.25", "10.0.0.2" ])

session = cluster.connect()

### spark-submit --master spark://10.0.0.24:7077 --packages org.apache.hadoop:hadoop-aws:2.7.3 --packages anguenot/pyspark-cassandra:0.9.0,com.databricks:spark-csv_2.10:1.2.0 --conf spark.cassandra.connection.host=18.236.219.153,54.190.116.213,34.212.36.241 --conf spark.akka.frameSize=1028 --py-files v0.7.0.zip --executor-memory 6g  --driver-memory 6g combined_cassandra.py

def start_spark_session():
    spark = SparkSession \
    .builder \
    .appName("Reddit DataFrame") \
    .config("spark.some.config.option", "some-value") \
    .getOrCreate()
    return spark

def get_reddit_df(directory_path):
    #read in jsons as pyspark df
    reddit_df = spark.read.json(reddit_directory_path)
    return reddit_df


def drop_irrelevant_columns(reddit_df):
    columns_to_drop = ['archived',
                       'author',
                       'author_cakeday',
                       'author_flair_background_color',
                       'author_flair_css_class',
                       'author_flair_richtext',
                       'author_flair_template_id',
                       'author_flair_text',
                       'author_flair_text_color',
                       'author_flair_type',
                       'author_fullname',
                       'can_gild',
                       'can_mod_post',
                       'collapsed',
                       'collapsed_reason',
                       'controversiality',
                       'distinguished',
                       'edited',
                       'gilded',
                       'id',
                       'is_submitter',
                       'link_id',
                       'no_follow',
                       'parent_id',
                       'removal_reason',
                       'retrieved_on',
                       'score_hidden',
                       'send_replies',
                       'stickied',
                       'subreddit_id',
                       'subreddit_name_prefixed',
                       'subreddit_type',
                       ]
    reddit_df = reddit_df.drop(*columns_to_drop)
    return reddit_df



def get_date_time_window_column(reddit_df):  
    #convert created at utc to string date and make new column
    reddit_df = reddit_df.withColumn('date_time', from_unixtime('author_created_utc'))
    #bin comments to 1 day windows and make new column saving the window
    reddit_df = reddit_df.withColumn(
    'day_window',
    window(
         col('date_time'), 
         windowDuration= '1 day'
    ).cast("struct<start:string,end:string>")
    )
    return reddit_df


def get_subreddit_topics_df(subreddit_topics_csv):
    #convert subreddit topics csv to spark df
    subreddit_topics = spark.read.csv(subreddit_topics_csv, header='true', inferSchema='true')
    return subreddit_topics


def get_subreddit_topics_column(reddit_df,subreddit_topics):
    reddit_df = reddit_df.join(broadcast(subreddit_topics), on = 'subreddit', how = 'left_outer')
    #clean dataframe
    reddit_df = reddit_df.filter(reddit_df.topic. isNotNull())
    #for topics with multiple topics split into single topics
    reddit_df = reddit_df.withColumn('topic', explode(split(col('topic'), ',')))
    return reddit_df

### HERE IS WHERE IT SHOULD BE EXPORTED TO ELASTIC SEARCH


def get_tokenized_df(reddit_df):
    #make comments all lowercase
    reddit_df = reddit_df.withColumn('word', explode(split(col('body'), '[\W_]+')))
    reddit_df = reddit_df.withColumn('word', lower(col('word')))
    reddit_df = reddit_df.filter( reddit_df['word'].rlike('[a-zA-Z]'))
    #duplicates dropped to ignore cases of someone using a word in the same post
    reddit_df = reddit_df.dropDuplicates()
    #order data in attempt to avoid request error
    reddit_df = reddit_df.orderBy(['word','day_window'],ascending=False) 
    return reddit_df

def get_partitioned_df(reddit_df):
    reddit_df = reddit_df.repartition("topic","word", "day_window")
    return reddit_df

def get_word_counts(reddit_df):                              
    #split comment body into indivdidual words at any nonword character, group by subreddit and day window 
    #reddit_df = reddit_df.orderBy(['word','day_window'],ascending=False)
    reddit_df = reddit_df\
    .groupBy('topic','day_window','word','date_time')\
    .count()
    return reddit_df


def get_word_counts_for_combined(reddit_df):   
    reddit_total_wc = reddit_df.groupby('word','day_window').sum()
    reddit_total_wc = reddit_total_wc.withColumnRenamed("sum(count)","count_per_day_all")
    reddit_df = reddit_df.join(reddit_total_wc, on = ['word','day_window'], how = 'left_outer')
    return reddit_df


def get_total_word_count_per_day_all(reddit_df):
    word_count_sum = reddit_df.groupBy('day_window').agg(sum('count'))
    word_count_sum = word_count_sum.withColumnRenamed("sum(count)","total_word_count_per_day_all")
    reddit_df = reddit_df.join(word_count_sum, on = ['day_window'], how = 'left_outer')
    return reddit_df


def get_total_word_count_per_day_topic(reddit_df):
    topic_count_sum = reddit_df.groupBy('day_window','topic').agg(sum('count'))
    topic_count_sum = topic_count_sum.withColumnRenamed("sum(count)","total_word_count_per_day_topic")
    reddit_df = reddit_df.join(topic_count_sum, on = ['day_window','topic'], how = 'left_outer')
    return reddit_df


def get_sub_freq_to_all_freq_ratio(reddit_df):
    #make sub_freq to all_freq ratio
    reddit_df = reddit_df.withColumn("sub_freq_to_all_freq_ratio", 
                         ((col("count")/col("total_word_count_per_day_topic"))/
                          (col("count_per_day_all")/col("total_word_count_per_day_all"))))
    return reddit_df


def get_rolling_average_of_sub_freq_to_all_freq_ratio(reddit_df):
    days = lambda i: i * 86400
    reddit_df = reddit_df.withColumn('timestamp', reddit_df.date_time.cast('timestamp'))
    w = (Window.orderBy(col('timestamp').cast('long')).rangeBetween(-days(2), 0))
    reddit_df = reddit_df.withColumn('rolling_average', avg("sub_freq_to_all_freq_ratio").over(w))
    reddit_df = reddit_df.drop('timestamp')
    return reddit_df


def get_change_in_rolling_average_per_day(reddit_df):
    #make column with previous day adjusted frequency
    #windowSpec = Window.orderBy(reddit_df['day_window'])
    windowSpec = \
     Window \
     .partitionBy(reddit_df['topic'])\
     .orderBy(reddit_df['day_window'])
    reddit_df = reddit_df.withColumn('prev_day_rolling_average',
                                    lag(reddit_df['rolling_average'])
                                    .over(windowSpec))
    reddit_df = reddit_df.withColumn('change_in_rolling_average', 
                                     (col('rolling_average') - col('rolling_average')))
    reddit_df = reddit_df.drop('prev_day_rolling_average')
    return reddit_df

 
def get_date_column(reddit_df):
    #get just date
    reddit_df = reddit_df.withColumn("date", to_date(col("date_time")))
    #remove uneeded columns
    columns_to_drop = ["day_window","date_time"]
    reddit_df = reddit_df.drop(*columns_to_drop)
    return reddit_df

def write_to_database(reddit_df):
    #make schema match with database
#    reddit_df = reddit_df.withColumn('total_word_count_per_day_topic', col('total_word_count_per_day_topic').cast('float'))
#    reddit_df = reddit_df.withColumn('total_word_count_per_day_all', col('total_word_count_per_day_all').cast('float'))
#    reddit_df = reddit_df.withColumn('total_word_count_per_day_topic', col('total_word_count_per_day_topic').cast('float'))
#    reddit_df = reddit_df.withColumn('count', col('count').cast('float'))
#    reddit_df = reddit_df.withColumn('count_per_day_all', col('count_per_day_all').cast('float'))
#    reddit_df = reddit_df.withColumn('date', col('count_per_day_all').cast('timestamp'))
#
#    
    reddit_df.write\
    .format("org.apache.spark.sql.cassandra")\
    .mode('append')\
    .options(table="reddit_results", keyspace="word")\
    .save()
    
#    url = "jdbc:postgresql://10.0.0.8:5431/word"
#    properties = {
#        "user": "jh",
#        "password": "jh",
#        "driver": "org.postgresql.Driver"
#    }
#    reddit_df.write.jdbc(url=url, table="reddit_results_9_24", mode= "append", properties=properties)
    
    
if __name__ == "__main__":
    spark = start_spark_session()
    reddit_directory_path = 's3a://jeff-halley-s3/split_reddit_comments_2018_07/xaa'
    subreddit_topics_csv = 's3a://jeff-halley-s3/split_reddit_comments_2018_07/subreddit_topics/subreddit_topics.csv'
    reddit_df = get_reddit_df(reddit_directory_path)
    #reddit_df = drop_irrelevant_columns(reddit_df)
    reddit_df = get_date_time_window_column(reddit_df)
    subreddit_topics = get_subreddit_topics_df(subreddit_topics_csv)
    reddit_df = get_subreddit_topics_column(reddit_df,subreddit_topics)
    reddit_df = get_tokenized_df(reddit_df)
    reddit_df = get_partitioned_df(reddit_df)
    reddit_df = get_word_counts(reddit_df)
    reddit_df = get_word_counts_for_combined(reddit_df)
    reddit_df = get_total_word_count_per_day_all(reddit_df)
    reddit_df = get_total_word_count_per_day_topic(reddit_df)
    reddit_df = get_sub_freq_to_all_freq_ratio(reddit_df)
    reddit_df = get_rolling_average_of_sub_freq_to_all_freq_ratio(reddit_df)
    reddit_df = get_change_in_rolling_average_per_day(reddit_df)
    reddit_df = get_date_column(reddit_df) 
    write_to_database(reddit_df)
    
    #reddit_rdd = reddit_df.rdd.map(tuple)
    #reddit_df.saveToCassandra("word", "test")
    #write_to_database(reddit_df)

#spark submit
# spark-submit --master spark://10.0.0.24:7077 --packages org.apache.hadoop:hadoop-aws:2.7.3 --conf spark.cassandra.connection.host=10.0.0.4,10.0.0.25,10.0.0.2 --packages datastax:spark-cassandra-connector:2.4.0-s_2.11 --conf spark.akka.frameSize=1028 --py-files v0.7.0.zip --executor-memory 6g  --driver-memory 6g combined_cassandra.py