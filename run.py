#!/usr/bin/env python
"""启动入口：python run.py  （--server 只开服务）"""
import sys

from app.main import main

if __name__ == "__main__":
    main(sys.argv[1:])
