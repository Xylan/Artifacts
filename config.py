#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Tue Jul 21 19:21:48 2026

@author: xylan
"""
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["ARTIFACTS_TOKEN"]
BASE_URL = "https://api.artifactsmmo.com"
DB_PATH = "artifacts_game.db"