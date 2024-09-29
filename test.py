# test_mongo_connection.py
import os
from pymongo import MongoClient
from dotenv import load_dotenv

load_dotenv()
mongo_uri = os.getenv("MONGO_URI")

try:
    client = MongoClient(mongo_uri)
    client.server_info()
    print("Connection Successful!")
except Exception as e:
    print("Connection Failed!")
    print(e)
