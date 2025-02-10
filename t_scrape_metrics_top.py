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
    Run top -b -n 1 -w 512 -c -o %CPU and parse:
      - overall CPU usage (from %Cpu(s) line)
      - top 5 processes by CPU usage (full command, %CPU)
    Returns a dict with:
        {
          "cpu_usage": float,
          "top_cpu": [(proc_name, cpu_usage), ... up to 5]
        }
    """
    cmd = ["top", "-b", "-n", "1", "-w", "512", "-c", "-o", "%CPU"]
    output = subprocess.check_output(cmd).decode("utf-8", errors="replace").splitlines()

    cpu_usage = 0.0
    top_cpu_list = []

    # Regex to find CPU usage in line like: "%Cpu(s):  3.4 us,  1.2 sy, ..."
    cpu_line_pattern = re.compile(r"^%Cpu\(s\):\s*(.*?)\s*us,.*")

    header_index = 0
    for i, line in enumerate(output):
        # Match CPU usage line
        match_cpu = cpu_line_pattern.match(line.strip())
        if match_cpu:
            try:
                # The matched group might look like "3.4" for user usage
                cpu_usage = float(match_cpu.group(1))
            except ValueError:
                cpu_usage = 0.0

        # Detect the table header line (commonly starts with "PID")
        if line.strip().startswith("PID"):
            header_index = i
            break

    # The process lines typically begin after the header line
    process_lines = output[header_index + 1 :]

    # Each process line typically has columns:
    #   PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ COMMAND
    # We'll parse the first 5. We'll split with maxsplit=11 to keep the full COMMAND (column 12).
    for line in process_lines[:5]:
        cols = line.split(None, 11)
        if len(cols) < 12:
            continue
        # command in cols[11], CPU usage in cols[8], etc.
        proc_name = cols[11]
        proc_cpu = cols[8]
        top_cpu_list.append((proc_name, proc_cpu))

    return {
        "cpu_usage": cpu_usage,
        "top_cpu": top_cpu_list
    }

def get_top_mem_info():
    """
    Run top -b -n 1 -w 512 -c -o %MEM and parse:
      - overall memory usage (as a percentage 0-100)
      - top 5 processes by memory usage (full command, %MEM)
    Returns dict with:
        {
          "mem_usage": float,           # in range 0..100
          "top_mem": [(proc_name, %MEM), ... up to 5]
        }
    """
    cmd = ["top", "-b", "-n", "1", "-w", "512", "-c", "-o", "%MEM"]
    output = subprocess.check_output(cmd).decode("utf-8", errors="replace").splitlines()

    mem_usage = 0.0
    top_mem_list = []

    # Example line in top output:
    #   MiB Mem :  15852.9 total,  13007.2 free,   1649.1 used,   196.7 buff/cache
    #
    # We'll parse total (group 1) and used (group 3).
    # If you want "used + buff/cache", adjust the formula below.
    mem_line_pattern = re.compile(
        r"^MiB Mem\s*:\s*(\S+)\s+total,\s*(\S+)\s+free,\s*(\S+)\s+used,\s*(\S+)\s+buff/cache"
    )

    header_index = 0
    for i, line in enumerate(output):
        match_mem = mem_line_pattern.match(line.strip())
        if match_mem:
            try:
                total_mib = float(match_mem.group(1))
                free_mib  = float(match_mem.group(2))
                used_mib  = float(match_mem.group(3))
                buff_mib  = float(match_mem.group(4))

                # Calculate memory usage in percentage (used / total * 100)
                # If you want to include buff/cache, do: (used_mib + buff_mib)
                mem_usage = (used_mib / total_mib) * 100
            except ValueError:
                mem_usage = 0.0

        # Detect the table header line (commonly starts with "PID")
        if line.strip().startswith("PID"):
            header_index = i
            break

    process_lines = output[header_index + 1 :]

    # Parse top 5 processes by memory usage
    for line in process_lines[:5]:
        cols = line.split(None, 11)
        if len(cols) < 12:
            continue
        proc_name = cols[11]
        proc_mem = cols[9]  # %MEM
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
