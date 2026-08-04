import sys
from tabulate import tabulate
import re
import logging
import csv
import getpass
import paramiko
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

def execute_ssh_command(remote_host, username, password, command):
    """Establishes an SSH connection and executes a CLI command."""
    try:
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(remote_host, username=username, password=password, timeout=15)
            stdin, stdout, stderr = ssh.exec_command(command)
            return stdout.read().decode(), stderr.read().decode()
    except Exception as e:
        logging.error(f"SSH connection or command execution failed on {remote_host}: {e}")
        sys.exit(1)

def convert_to_gb(size_str):
    """Convert a storage size string (TB/GB/MB) into a uniform float value in GB."""
    size_str = size_str.strip()
    if 'TB' in size_str:
        return float(size_str.replace('TB', '')) * 1024
    elif 'GB' in size_str:
        return float(size_str.replace('GB', ''))
    elif 'MB' in size_str:
        return float(size_str.replace('MB', '')) / 1024
    else:
        try:
            return float(size_str)
        except ValueError:
            return 0.0

def parse_aggregate_summary(volume_output, aggregate_output, remote_host):
    """Processes the raw command lines, aligns volume/aggregate data, and generates reports."""
    # Split raw terminal blocks into distinct lines and remove completely blank rows
    volume_lines = [l.strip() for l in volume_output.strip().split('\n') if l.strip()]
    aggregate_lines = [l.strip() for l in aggregate_output.strip().split('\n') if l.strip()]

    aggregates = {}
    headers = [
        "Aggregate", "Total Volume Allocated (GB)", "Total Volume Used (GB)", 
        "Total Volume Available (GB)", "Aggregate Size (GB)", "Aggregate Used Size (GB)", 
        "Aggregate Available Size (GB)", "% Logically Allocated", "% Logically Used", "Aggregate % Used"
    ]

    # --- Step 1: Parse Physical Aggregate Data ---
    aggregate_info = {}
    for line in aggregate_lines:
        # Intelligently bypass header lines, system divider rows, or total summary footers
        if any(x in line for x in ["aggregate", "---", "entries were displayed"]):
            continue
            
        parts = re.split(r'\s{2,}|\s(?=\d)', line)
        if len(parts) < 5:
            continue
            
        aggr_name, aggr_avail, aggr_percent_used, aggr_size, aggr_used = parts
        try:
            aggr_avail = convert_to_gb(aggr_avail)
            aggr_percent_used = float(aggr_percent_used.replace('%', ''))
            aggr_size = convert_to_gb(aggr_size)
            aggr_used = convert_to_gb(aggr_used)
        except ValueError:
            logging.warning(f"Could not parse physical capacities for line: {line}")
            continue
            
        aggregate_info[aggr_name] = {
            "size": aggr_size, 
            "used": aggr_used, 
            "available": aggr_avail, 
            "percent_used": aggr_percent_used
        }

    # --- Step 2: Parse Logical Volume Data ---
    for line in volume_lines:
        if any(x in line for x in ["vserver", "---", "entries were displayed"]):
            continue
            
        parts = line.split()
        if len(parts) < 5:
            continue
            
        # Extract metrics safely from right-to-left to prevent cluster/SVM space mismatches
        try:
            available = parts[-1]
            size = parts[-2]
            aggr_name = parts[-3]
            
            # Guard against structural non-data entries
            if not any(unit in size for unit in ['TB', 'GB', 'MB']):
                continue
                
            allocated = convert_to_gb(size)
            avail = convert_to_gb(available) if available != '-' else 0
            used = allocated - avail
        except Exception:
            continue
            
        if aggr_name not in aggregates:
            aggregates[aggr_name] = {"total_allocated": 0, "total_used": 0, "total_available": 0}
            
        aggregates[aggr_name]["total_allocated"] += allocated
        aggregates[aggr_name]["total_used"] += used
        aggregates[aggr_name]["total_available"] += avail

    # --- Step 3: Match Datasets & Build Final Summary Data ---
    summary_data = []
    for aggr_name, data in aggregates.items():
        if aggr_name not in aggregate_info:
            logging.warning(f"Aggregate mapping data missing for '{aggr_name}'. Skipping line details.")
            continue
            
        total_allocated, total_used, total_available = data.values()
        aggr_size = aggregate_info[aggr_name]["size"]
        aggr_used = aggregate_info[aggr_name]["used"]
        aggr_avail = aggregate_info[aggr_name]["available"]
        aggr_percent_used = aggregate_info[aggr_name]["percent_used"]
        
        if aggr_size == 0:
            logging.warning(f"Aggregate '{aggr_name}' size registered as 0. Percentage dropped.")
            continue
            
        percent_allocated = (total_allocated / aggr_size) * 100
        percent_used = (total_used / aggr_size) * 100
        
        summary_data.append([
            aggr_name, 
            f"{total_allocated:.2f}", 
            f"{total_used:.2f}", 
            f"{total_available:.2f}", 
            f"{aggr_size:.2f}", 
            f"{aggr_used:.2f}", 
            f"{aggr_avail:.2f}", 
            f"{percent_allocated:.2f}%", 
            f"{percent_used:.2f}%", 
            f"{aggr_percent_used:.2f}%"
        ])

    # Sort rows systematically by controller domain name
    summary_data.sort(key=lambda x: x[0].split('_')[0])

    # Add descriptive calculation footer summary
    summary_data.append(["Total Aggregates", len(summary_data), "", "", "", "", "", "", "", ""])

    # Output interactive layout grid directly to shell terminal
    print("\n" + tabulate(summary_data, headers=headers, tablefmt="grid"))

    # Write persistent CSV report file
    date_str = datetime.now().strftime("%Y%m%d")
    csv_filename = f"aggr-capacity-{remote_host}-{date_str}.csv"
    try:
        with open(csv_filename, 'w', newline='') as csvfile:
            csvwriter = csv.writer(csvfile)
            csvwriter.writerow(headers)
            csvwriter.writerows(summary_data)
        print(f"\nSuccessfully exported unified capacity report to: {csv_filename}")
    except IOError as e:
        logging.error(f"Failed to generate CSV export file: {e}")

if __name__ == "__main__":
    # Interactive input routine
    remote_host = input("Enter the remote host IP or hostname: ")
    username = input("Enter your username: ")
    password = getpass.getpass("Enter your password: ")

    print(f"\nConnecting to {remote_host}...")
    
    # Execution Step 1: Gather volume layouts
    print("Executing: volume show...")
    volume_output, volume_error = execute_ssh_command(
        remote_host, username, password, 
        "volume show -fields volume,aggregate,size,available"
    )
    if volume_error:
        logging.warning(f"Standard Error trace caught from Volume command:\n{volume_error}")

    # Execution Step 2: Gather underlying physical hardware blocks
    print("Executing: aggr show...")
    aggregate_output, aggregate_error = execute_ssh_command(
        remote_host, username, password, 
        "aggr show -fields availsize,percent-used,size,usedsize"
    )
    if aggregate_error:
        logging.warning(f"Standard Error trace caught from Aggregate command:\n{aggregate_error}")

    # Process and build storage reporting charts
    print("Compiling data fields and computing cross-allocations...\n")
    parse_aggregate_summary(volume_output, aggregate_output, remote_host)