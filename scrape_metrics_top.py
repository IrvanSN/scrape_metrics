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
    Get overall CPU usage using psutil and top processes by CPU usage.

    Returns a dict with:
        {
          "cpu_usage": float,
          "top_cpu": [(proc_name, cpu_usage), ... up to 5]
        }
    """
    # Use psutil for a reliable overall CPU usage percentage.
    cpu_usage = psutil.cpu_percent(interval=0.1)

    # Run top to get per-process CPU info.
    cmd = ["top", "-b", "-n", "1", "-w", "512", "-c", "-o", "%CPU"]
    output = subprocess.check_output(cmd).decode("utf-8", errors="replace").splitlines()

    # Find the header line (typically starts with "PID") and then parse the next five lines.
    header_index = 0
    for i, line in enumerate(output):
        if line.strip().startswith("PID"):
            header_index = i
            break

    process_lines = output[header_index + 1:]
    top_cpu_list = []
    for line in process_lines[:5]:
        cols = line.split(None, 11)
        if len(cols) < 12:
            continue
        proc_name = cols[11]
        proc_cpu = cols[8]  # %CPU column
        top_cpu_list.append((proc_name, proc_cpu))

    return {
        "cpu_usage": cpu_usage,
        "top_cpu": top_cpu_list
    }

def get_top_mem_info():
    """
    Get overall memory usage using psutil and top processes by memory usage.

    Returns dict with:
        {
          "mem_usage": float,           # in percentage (0-100)
          "top_mem": [(proc_name, %MEM), ... up to 5]
        }
    """
    # Use psutil for reliable overall memory usage percentage.
    mem_usage = psutil.virtual_memory().percent

    # Run top to get per-process memory info.
    cmd = ["top", "-b", "-n", "1", "-w", "512", "-c", "-o", "%MEM"]
    output = subprocess.check_output(cmd).decode("utf-8", errors="replace").splitlines()

    header_index = 0
    for i, line in enumerate(output):
        if line.strip().startswith("PID"):
            header_index = i
            break

    process_lines = output[header_index + 1:]
    top_mem_list = []
    for line in process_lines[:5]:
        cols = line.split(None, 11)
        if len(cols) < 12:
            continue
        proc_name = cols[11]
        proc_mem = cols[9]  # %MEM column
        top_mem_list.append((proc_name, proc_mem))

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
        f"{cpu_usage:.2f}",
        *top_cpu_fields,     # 10 fields (5 name+usage pairs)
        f"{mem_usage:.2f}",
        *top_mem_fields,     # 10 fields
        str(nginx_active_connections),
        f"{nginx_requests_ps:.2f}"
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
