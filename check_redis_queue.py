#!/usr/bin/env python3
"""Check Redis queue status."""

import redis
import os
import json
from dotenv import load_dotenv

load_dotenv()

def check_redis_queues():
    # Connect to Redis
    redis_url = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
    r = redis.from_url(redis_url)
    
    # Check queue lengths
    queues = ['downloads', 'transcription', 'summarization', 'emails']
    
    print("Redis Queue Status:")
    print("=" * 40)
    
    for queue in queues:
        queue_name = f"celery_{queue}"
        length = r.llen(queue_name)
        print(f"{queue:15}: {length} tasks")
        
        # Show first few tasks in each queue
        if length > 0:
            tasks = r.lrange(queue_name, 0, min(2, length-1))
            for i, task in enumerate(tasks):
                try:
                    task_data = json.loads(task)
                    task_name = task_data.get('task', 'unknown')
                    task_id = task_data.get('id', 'unknown')
                    print(f"  {i+1}. {task_name} ({task_id[:8]}...)")
                except:
                    print(f"  {i+1}. [unparseable task]")
    
    print("\nRedis Connection:", "✅ Connected" if r.ping() else "❌ Failed")

if __name__ == '__main__':
    check_redis_queues()