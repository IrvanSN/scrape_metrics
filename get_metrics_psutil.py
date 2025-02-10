#!/usr/bin/env python3
import psutil
import subprocess
import re
import datetime
import requests  # pip install requests
import time
import os
import json

# Configuration
NGINX_STATUS_URL = "http://127.0.0.1/stub_status"
OUTPUT_FILE = "/tmp/system_stats.csv"
STATE_FILE = "/tmp/.nginx_requests_state.json"  # To store last requests count & timestamp

def get_top_cpu_info():
    """
    Use psutil to get:
      - overall CPU usage
      - top 5 processes by CPU usage (command and %CPU)
    """
    # overall CPU usage (in %)
    cpu_usage = psutil.cpu_percent(interval=0.1)

    # gather processes with CPU usage
    # Note: calling cpu_percent() *immediately* after process_iter() can be 0.0
    #       so an interval above helps usage to settle.
    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline', 'cpu_percent']):
        try:
            info = p.info
            # By default, psutil returns the CPU usage since last call.
            # We already gave a small interval=0.1, so it should be populated.
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # sort descending by cpu_percent
    procs.sort(key=lambda x: x['cpu_percent'], reverse=True)

    # Build the top 5 list
    top_cpu_list = []
    for proc in procs[:5]:
        cmd = " ".join(proc['cmdline']) if proc['cmdline'] else proc['name']
        cpu_str = f"{proc['cpu_percent']:.1f}"
        top_cpu_list.append((cmd, cpu_str))

    return {
        "cpu_usage": cpu_usage,
        "top_cpu": top_cpu_list
    }

def get_top_mem_info():
    """
    Use psutil to get:
      - overall memory usage in %
      - top 5 processes by memory usage (command and %MEM)
    """
    # overall memory usage
    mem = psutil.virtual_memory()
    mem_usage = mem.percent

    procs = []
    for p in psutil.process_iter(['pid', 'name', 'cmdline', 'memory_percent']):
        try:
            info = p.info
            procs.append(info)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    # sort descending by memory_percent
    procs.sort(key=lambda x: x['memory_percent'], reverse=True)

    top_mem_list = []
    for proc in procs[:5]:
        cmd = " ".join(proc['cmdline']) if proc['cmdline'] else proc['name']
        mem_str = f"{proc['memory_percent']:.1f}"
        top_mem_list.append((cmd, mem_str))

    return {
        "mem_usage": mem_usage,
        "top_mem": top_mem_list
    }

def get_nginx_stub_status(url=NGINX_STATUS_URL):
    """
    Fetch the nginx stub status page and parse:
      - active_connections (int)
      - total_requests (int)  -> cumulative requests

    If there's any error, return (0, 0).
    """
    try:
        response = requests.get(url, timeout=2)
        response.raise_for_status()
    except requests.RequestException:
        # Return 0 if there's any error
        return 0, 0

    text = response.text.strip()
    if not text:
        return 0, 0

    lines = text.split('\n')
    if len(lines) < 3:
        return 0, 0

    active_connections = 0
    total_requests = 0

    # Attempt to parse active connections
    try:
        for line in lines:
            if line.startswith("Active connections:"):
                parts = line.split()
                if len(parts) >= 3:
                    active_connections = int(parts[2])
                break
    except (ValueError, IndexError):
        active_connections = 0

    # Attempt to parse total requests
    # Typically in the stub, the 3rd line has "server accepts handled requests"
    # The next line might be something like " 1234 1234 2468"
    try:
        line_with_requests = lines[2].strip()
        parts = line_with_requests.split()
        if len(parts) == 3:
            total_requests = int(parts[2])
    except (ValueError, IndexError):
        total_requests = 0

    return active_connections, total_requests

def compute_requests_per_second(current_requests):
    """
    Reads last known requests count & timestamp from STATE_FILE.
    Returns the requests_per_second and updates the state file
    with the current requests count and current timestamp.

    If there's no prior state, returns 0.0 on first run.
    """
    now = time.time()

    # If no state file, create it
    if not os.path.isfile(STATE_FILE):
        with open(STATE_FILE, 'w') as f:
            json.dump({"last_requests": current_requests, "last_time": now}, f)
        return 0.0

    # Read the previous state
    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
            last_requests = state.get("last_requests", 0)
            last_time = state.get("last_time", now)
    except (json.JSONDecodeError, FileNotFoundError):
        # If file is corrupted or not found
        last_requests = current_requests
        last_time = now

    elapsed = now - last_time if now > last_time else 1.0
    diff_requests = current_requests - last_requests
    if diff_requests < 0:
        # if total_requests resets, we clamp to 0
        diff_requests = 0

    rps = diff_requests / elapsed

    # Update state
    with open(STATE_FILE, 'w') as f:
        json.dump({"last_requests": current_requests, "last_time": now}, f)

    return rps

def main():
    # 1. Get CPU info
    cpu_info = get_top_cpu_info()
    cpu_usage = cpu_info["cpu_usage"]
    top_cpu = cpu_info["top_cpu"]

    # 2. Get MEM info
    mem_info = get_top_mem_info()
    mem_usage = mem_info["mem_usage"]
    top_mem = mem_info["top_mem"]

    # 3. Get NGINX stub info
    nginx_active_connections, nginx_total_requests = get_nginx_stub_status()

    # Compute requests per second
    nginx_requests_ps = compute_requests_per_second(nginx_total_requests)

    # Flatten top CPU and top MEM info into CSV-friendly fields
    top_cpu_fields = []
    for i in range(5):
        if i < len(top_cpu):
            proc_name, proc_val = top_cpu[i]
        else:
            proc_name, proc_val = "", ""
        top_cpu_fields.append(proc_name)
        top_cpu_fields.append(proc_val)

    top_mem_fields = []
    for i in range(5):
        if i < len(top_mem):
            proc_name, proc_val = top_mem[i]
        else:
            proc_name, proc_val = "", ""
        top_mem_fields.append(proc_name)
        top_mem_fields.append(proc_val)

    # Prepare CSV line with ";" as separator
    now_str = datetime.datetime.now().isoformat()

    # Example columns:
    # [ timestamp, cpu_usage, top_1_cpu_proc_name, top_1_cpu_proc_usage, ...,
    #   mem_usage, top_1_mem_proc_name, top_1_mem_proc_usage, ...,
    #   nginx_active_connections, nginx_requests_ps ]
    row_items = [
        now_str,
        str(f"{cpu_usage:.2f}"),
        *top_cpu_fields,     # 10 fields (5 name+usage pairs)
        str(f"{mem_usage:.2f}"),
        *top_mem_fields,     # 10 fields
        str(nginx_active_connections),
        str(f"{nginx_requests_ps:.2f}")
    ]

    # Join with ';'
    csv_line = ";".join(row_items) + "\n"

    # Print to console
    print(csv_line, end="")

    # Append to file
    with open(OUTPUT_FILE, "a") as f:
        f.write(csv_line)

if __name__ == "__main__":
    main()
