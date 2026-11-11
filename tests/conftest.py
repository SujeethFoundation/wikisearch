import os
import sys

# Importing function_app must never start a real read of GitHub or touch storage.
os.environ["PRELOAD_ON_START"] = "false"
os.environ.pop("WIKI_SNAPSHOT_CONNECTION_STRING", None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
