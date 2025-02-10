#!/usr/bin/env python3
import psutil
import requests
import time
import json
import os
from datetime import datetime

# Configuration
NGINX_STATUS_URL = "http://127.0.0.1/nginx_status"
OUTPUT_CSV_FILE = "/tmp/system_stats.csv"
STATE_FILE = "/tmp/.nginx_requests_state.json"  # To store last requests count & timestamp

# You can tune this for faster/slower sampling
CPU_SAMPLING_INTERVAL = 0.2  # 200ms sampling

# Timeout for NGINX stub status requests
NGINX_TIMEOUT = 1  # seconds

###############################################################################
#                           NGINX Stub Status
###############################################################################
def get_nginx_stub_status(url=NGINX_STATUS_URL):
    """
    Fetch nginx stub status page:
      - active_connections (int)
      - total_requests (int)

    If error or parse failure, return (0, 0).
    """
    try:
        resp = requests.get(url, timeout=NGINX_TIMEOUT)
        resp.raise_for_status()
        text = resp.text.strip()
    except Exception:
        return 0, 0  # fallback

    lines = text.split('\n')
    if len(lines) < 3:
        return 0, 0

    active_connections = 0
    total_requests = 0

    # Example:
    # Active connections: 291
    # server accepts handled requests
    #  16630948 16630948 31070481
    # Reading: 21 Writing: 2 Waiting: 21

    # Parse active connections
    for line in lines:
        if line.startswith("Active connections:"):
            parts = line.split()
            try:
                active_connections = int(parts[2])
            except (ValueError, IndexError):
                active_connections = 0
            break

    # Parse total requests (the 3rd number in the 3rd line)
    try:
        parts = lines[2].strip().split()
        if len(parts) == 3:
            total_requests = int(parts[2])
    except (ValueError, IndexError):
        total_requests = 0

    return active_connections, total_requests

def compute_requests_per_second(current_requests):
    """
    Uses STATE_FILE to store (last_requests, last_time).
    If no previous data, returns 0.0 and initializes.
    """
    now = time.time()

    if not os.path.isfile(STATE_FILE):
        # Initialize state
        with open(STATE_FILE, 'w') as f:
            json.dump({"last_requests": current_requests, "last_time": now}, f)
        return 0.0

    try:
        with open(STATE_FILE, 'r') as f:
            state = json.load(f)
        last_requests = state.get("last_requests", 0)
        last_time = state.get("last_time", now)
    except (json.JSONDecodeError, FileNotFoundError):
        last_requests = current_requests
        last_time = now

    elapsed = now - last_time if now > last_time else 1.0
    diff = current_requests - last_requests
    if diff < 0:
        diff = 0

    rps = diff / elapsed

    # Update state
    with open(STATE_FILE, 'w') as f:
        json.dump({"last_requests": current_requests, "last_time": now}, f)

    return rps

###############################################################################
#                           System Stats (psutil)
###############################################################################
def get_system_cpu_usage(interval=CPU_SAMPLING_INTERVAL):
    """
    Get overall CPU usage with a short sampling interval.
    The lower the interval, the faster (but less accurate).
    """
    return psutil.cpu_percent(interval=interval)

def get_system_memory_usage():
    """
    Returns overall memory usage as a percentage.
    """
    return psutil.virtual_memory().percent

def get_top_processes_by_cpu(n=5):
    """
    Single-pass approach:
      - We'll call proc.cpu_percent(interval=None) for each process
        AFTER calling psutil.cpu_percent(interval=X) for the system.
      - This yields CPU usage since last call, but no separate warm-up pass.
      - We then sort by CPU usage and pick top n.

    This approach is faster than a full 1-second or 2-pass warm-up.
    """
    processes = []
    for proc in psutil.process_iter(['name']):
        try:
            name = proc.info['name'] if proc.info['name'] else "-."
            # CPU usage since last call (which was after the system cpu_percent() call)
            cpu_val = proc.cpu_percent(None)
            processes.append((name, cpu_val))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            processes.append(("-.", 0.0))

    # Sort descending
    processes.sort(key=lambda x: x[1], reverse=True)
    return processes[:n]

def get_top_processes_by_memory(n=5):
    """
    Memory usage is instantaneous, so no warm-up needed.
    """
    processes = []
    for proc in psutil.process_iter(['name', 'memory_percent']):
        try:
            name = proc.info['name'] if proc.info['name'] else "-."
            mem_val = proc.info['memory_percent'] if proc.info['memory_percent'] else 0.0
            processes.append((name, mem_val))
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            processes.append(("-.", 0.0))

    processes.sort(key=lambda x: x[1], reverse=True)
    return processes[:n]

###############################################################################
#                           Main
###############################################################################
def main():
    start_time = time.time()

    # 1) Timestamp
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 2) CPU usage with short interval (e.g. 0.2s)
    cpu_usage = get_system_cpu_usage(interval=CPU_SAMPLING_INTERVAL)

    # 3) Memory usage (instant)
    mem_usage = get_system_memory_usage()

    # 4) Top processes by CPU (single-pass, no extra sleeps)
    top_cpu = get_top_processes_by_cpu(5)
    while len(top_cpu) < 5:
        top_cpu.append(("-.", 0.0))

    # 5) Top processes by Memory
    top_mem = get_top_processes_by_memory(5)
    while len(top_mem) < 5:
        top_mem.append(("-.", 0.0))

    # 6) NGINX stub status
    nginx_active_connections, nginx_total_requests = get_nginx_stub_status()
    nginx_requests_ps = compute_requests_per_second(nginx_total_requests)

    # Prepare CSV row (semicolon separated)
    # Format:
    #   timestamp;
    #   cpu_usage;
    #   top_1_cpu_proc_name; top_1_cpu_proc_usage;
    #   ...
    #   top_5_cpu_proc_name; top_5_cpu_proc_usage;
    #   mem_usage;
    #   top_1_mem_proc_name; top_1_mem_proc_usage;
    #   ...
    #   top_5_mem_proc_name; top_5_mem_proc_usage;
    #   nginx_active_connections;
    #   nginx_requests_ps

    cpu_cols = []
    for proc_name, cpu_val in top_cpu:
        cpu_cols.append(proc_name)
        cpu_cols.append(f"{cpu_val:.2f}")

    mem_cols = []
    for proc_name, mem_val in top_mem:
        mem_cols.append(proc_name)
        mem_cols.append(f"{mem_val:.2f}")

    row_data = [
        timestamp,
        f"{cpu_usage:.2f}"
    ]
    row_data.extend(cpu_cols)
    row_data.append(f"{mem_usage:.2f}")
    row_data.extend(mem_cols)
    row_data.append(str(nginx_active_connections))
    row_data.append(f"{nginx_requests_ps:.2f}")

    csv_line = ";".join(row_data)

    # Append to file
    with open(OUTPUT_CSV_FILE, "a") as f:
        f.write(csv_line + "\n")

    # Print runtime (optional)
    end_time = time.time()
    elapsed = end_time - start_time
    print(f"Appended row to {OUTPUT_CSV_FILE} in {elapsed:.2f}s")
    print(csv_line)

if __name__ == "__main__":
    main()
