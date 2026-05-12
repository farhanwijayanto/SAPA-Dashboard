from pymongo import MongoClient
import os


_client = None


def get_mongo_client() -> MongoClient:
    global _client
    if _client is not None:
        return _client
    uri = os.getenv("MONGODB_URI", "mongodb://localhost:27017")
    _client = MongoClient(uri, serverSelectionTimeoutMS=3000)
    return _client


def get_logs_collection():
    db_name = os.getenv("MONGODB_DB", "sapa")
    collection_name = os.getenv("MONGODB_COLLECTION_ATTENDANCE", "attendance_logs")
    return get_mongo_client()[db_name][collection_name]


def get_audit_collection():
    db_name = os.getenv("MONGODB_DB", "sapa")
    collection_name = os.getenv("MONGODB_COLLECTION_AUDIT", "audit_logs")
    return get_mongo_client()[db_name][collection_name]
