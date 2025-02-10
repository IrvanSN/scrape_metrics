#!/usr/bin/env python3
import psutil
import requests
import os
import json
import time
from datetime import datetime

# Configuration
NGINX_STATUS_URL = "http://127.0.0.1/stub_status"
OUTPUT_CSV_FILE = "/tmp/host_anomaly_detection.csv"
STATE_FILE = "/tmp/.nginx_requests_state.json"  # To store last requests count & timestamp

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

def get_cpu_usage():
    """Returns the overall CPU usage percent."""
    return psutil.cpu_percent(interval=1)

def get_mem_usage():
    """Returns the overall memory usage percent."""
    return psutil.virtual_memory().percent

def get_top_processes_by_cpu(n=5):
    """
    Returns a list of (process_name, cpu_usage_percent) for the top n processes by CPU usage
    using a two-pass approach to get more accurate CPU usage data.
    """
    # --- First pass: "warm up" all processes by calling cpu_percent(None)
    # so psutil starts measuring their usage.
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            proc.cpu_percent(None)  # initialize
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    # Wait 1 second (or however long you want) so psutil can measure usage
    time.sleep(1)

    # --- Second pass: now retrieve the current CPU usage since last call
    process_usage = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = proc.info['name'] if proc.info['name'] else "-."
            cpu_pct = proc.cpu_percent(None)  # usage since last call
            process_usage.append((name, cpu_pct))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            process_usage.append(("-.", 0.0))

    # Sort by CPU usage descending, then take top n
    process_usage.sort(key=lambda x: x[1], reverse=True)
    return process_usage[:n]

def get_top_processes_by_memory(n=5):
    """
    Returns a list of (process_name, memory_usage_percent) for the top n processes by memory usage
    using a two-pass approach. Memory usage is typically an instantaneous metric, but this
    replicates the same approach for consistency.
    """

    # --- First pass (optional “warm up”):
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            proc.memory_percent()  # call once
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            pass

    # Short wait for consistency (not strictly necessary for memory)
    time.sleep(1)

    # --- Second pass: now retrieve memory usage
    process_usage = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = proc.info['name'] if proc.info['name'] else "-."
            mem_pct = proc.memory_percent()
            process_usage.append((name, mem_pct))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            process_usage.append(("-.", 0.0))

    # Sort by memory usage descending, take top n
    process_usage.sort(key=lambda x: x[1], reverse=True)
    return process_usage[:n]

def main():
    # Current timestamp (human-readable)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. CPU Usage
    cpu_usage = get_cpu_usage()

    # 2. Top 5 CPU processes
    top_cpu_procs = get_top_processes_by_cpu(5)
    # Pad if fewer than 5
    while len(top_cpu_procs) < 5:
        top_cpu_procs.append(("-.", 0.0))

    # 3. Memory Usage
    mem_usage = get_mem_usage()

    # 4. Top 5 Memory processes
    top_mem_procs = get_top_processes_by_memory(5)
    # Pad if fewer than 5
    while len(top_mem_procs) < 5:
        top_mem_procs.append(("-.", 0.0))

    # 5. NGINX stub status: active connections, requests per second
    nginx_active_connections, nginx_total_requests = get_nginx_stub_status()

    # Compute requests per second
    nginx_requests_ps = compute_requests_per_second(nginx_total_requests)

    # Prepare CSV row, separated by semicolons
    # Format:
    # timestamp; cpu_usage;
    # top_1_cpu_proc_name; top_1_cpu_proc_usage;
    # top_2_cpu_proc_name; top_2_cpu_proc_usage;
    # top_3_cpu_proc_name; top_3_cpu_proc_usage;
    # top_4_cpu_proc_name; top_4_cpu_proc_usage;
    # top_5_cpu_proc_name; top_5_cpu_proc_usage;
    # mem_usage;
    # top_1_mem_proc_name; top_1_mem_proc_usage;
    # top_2_mem_proc_name; top_2_mem_proc_usage;
    # top_3_mem_proc_name; top_3_mem_proc_usage;
    # top_4_mem_proc_name; top_4_mem_proc_usage;
    # top_5_mem_proc_name; top_5_mem_proc_usage;
    # nginx_active_connections; nginx_requests_ps

    cpu_cols = []
    for proc_name, cpu_percent in top_cpu_procs:
        cpu_cols.append(proc_name if proc_name else "-.")
        cpu_cols.append(f"{cpu_percent:.2f}")

    mem_cols = []
    for proc_name, mem_percent in top_mem_procs:
        mem_cols.append(proc_name if proc_name else "-.")
        mem_cols.append(f"{mem_percent:.2f}")

    row_data = [
        timestamp,
        f"{cpu_usage:.2f}"
    ]
    row_data.extend(cpu_cols)
    row_data.append(f"{mem_usage:.2f}")
    row_data.extend(mem_cols)
    row_data.append(str(nginx_active_connections))
    row_data.append(f"{nginx_requests_ps:.2f}")

    # Convert to semicolon-separated string
    csv_line = ";".join(row_data)

    # Append to file
    with open(OUTPUT_CSV_FILE, "a") as f:
        f.write(csv_line + "\n")

    print(csv_line)

if __name__ == "__main__":
    main()
