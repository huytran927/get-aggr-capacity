#!/usr/bin/env python3
import sys
import os
from tabulate import tabulate
import re
import logging
import csv
import json
import getpass
import paramiko
from datetime import datetime
# Configure logging
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
def normalize_credential(value):
    """Fix common copy/paste artifacts in credential env vars: curly/smart
    quotes wrapping the value (e.g. from pasting out of a notes app), and
    stray leading/trailing whitespace. Does NOT touch a literal backslash-n
    inside the value -- for domain-prefixed logins like 'account-01\\n028724'
    that backslash is a literal domain separator character, not an escaped
    newline, and converting it would break authentication."""
    if value is None:
        return value
    value = value.strip('\u201c\u201d\u2018\u2019"\'').strip()
    return value
def execute_ssh_command(remote_host, username, password, command):
    """Establishes an SSH connection and executes a CLI command."""
    try:
        with paramiko.SSHClient() as ssh:
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                # Establish the transport without letting paramiko silently
                # attempt password/agent/key auth first -- we want to drive
                # keyboard-interactive ourselves so prompts can be logged.
                ssh.connect(
                    remote_host, username=username, password=None,
                    allow_agent=False, look_for_keys=False, timeout=15
                )
            except paramiko.ssh_exception.SSHException:
                pass  # expected: "none" auth is rejected, transport stays open

            transport = ssh.get_transport()
            if transport is None:
                raise RuntimeError("Failed to establish SSH transport.")

            if not transport.is_authenticated():
                def interactive_handler(title, instructions, prompt_list):
                    if title:
                        logging.info(f"Keyboard-interactive title: {title}")
                    if instructions:
                        logging.info(f"Keyboard-interactive instructions: {instructions}")
                    answers = []
                    for prompt_text, echo in prompt_list:
                        logging.info(f"Keyboard-interactive prompt received: {prompt_text!r} (echo={echo})")
                        answers.append(password)
                    return answers

                transport.auth_interactive(username, interactive_handler)

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
def parse_aggregate_summary(volume_output, aggregate_output, efficiency_output, remote_host, selected_env="production"):
    """Processes raw output across Volumes, Aggregates, and Efficiency metrics."""
    volume_lines = [l.strip() for l in volume_output.strip().split('\n') if l.strip()]
    aggregate_lines = [l.strip() for l in aggregate_output.strip().split('\n') if l.strip()]
    
    efficiency_raw = efficiency_output.strip()
    aggregates = {}
    headers = [
        "Aggregate", "Total Vol Allocated (GB)", "Total Vol Used (GB)", 
        "Total Vol Avail (GB)", "Aggr Size (GB)", "Aggr Used (GB)", 
        "Aggr Avail (GB)", "% Logically Alloc", "% Logically Used", 
        "Aggr % Used", "Total Efficiency", "Data Reduction (w/o Snaps)"
    ]
    # --- Step 1: Parse Physical Aggregate Data ---
    aggregate_info = {}
    for line in aggregate_lines:
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
            continue
            
        aggregate_info[aggr_name] = {
            "size": aggr_size, 
            "used": aggr_used, 
            "available": aggr_avail, 
            "percent_used": aggr_percent_used,
            "total_eff": "1.00:1",      
            "data_red": "1.00:1"
        }
    # --- Step 2: Parse Efficiency Ratios (Block Parsing) ---
    aggr_blocks = efficiency_raw.split("Aggregate: ")
    for block in aggr_blocks:
        if not block.strip() or "Node:" not in block:
            continue
            
        lines = [l.strip() for l in block.split('\n') if l.strip()]
        aggr_name = lines[0].split()[0]  
        
        total_eff = "1.00:1"
        data_red = "1.00:1"
        
        for line in lines:
            if "Total Storage Efficiency Ratio:" in line:
                total_eff = line.split(":")[-2].strip() + ":" + line.split(":")[-1].strip()
                total_eff = re.sub(r'\s+', '', total_eff)
            elif "Total Data Reduction Efficiency Ratio w/o Snapshots:" in line:
                data_red = line.split(":")[-2].strip() + ":" + line.split(":")[-1].strip()
                data_red = re.sub(r'\s+', '', data_red)
                
        if aggr_name in aggregate_info:
            aggregate_info[aggr_name]["total_eff"] = total_eff
            aggregate_info[aggr_name]["data_red"] = data_red
    # --- Step 3: Parse Logical Volume Data ---
    # Tracking raw logical details for JSON mapping payload
    parsed_volumes_list = []
    
    for line in volume_lines:
        if any(x in line for x in ["vserver", "---", "entries were displayed"]):
            continue
            
        parts = line.split()
        if len(parts) < 5:
            continue
            
        try:
            # Assumes output pattern: vserver volume aggregate size available
            vserver = parts[0]
            vol_name = parts[1]
            available = parts[-1]
            size = parts[-2]
            aggr_name = parts[-3]
            
            if not any(unit in size for unit in ['TB', 'GB', 'MB']):
                continue
                
            allocated = convert_to_gb(size)
            avail = convert_to_gb(available) if available != '-' else 0
            used = allocated - avail
            
            parsed_volumes_list.append({
                "vserver": vserver,
                "volume": vol_name,
                "aggregate": aggr_name,
                "allocated_gb": round(allocated, 2),
                "used_gb": round(used, 2),
                "available_gb": round(avail, 2)
            })
            
        except Exception:
            continue
            
        if aggr_name not in aggregates:
            aggregates[aggr_name] = {"total_allocated": 0, "total_used": 0, "total_available": 0}
            
        aggregates[aggr_name]["total_allocated"] += allocated
        aggregates[aggr_name]["total_used"] += used
        aggregates[aggr_name]["total_available"] += avail
    # --- Step 4: Combine Data Matrices & Build Target JSON Map ---
    summary_data = []
    aggr_json_map = {}
    running_total_size_gb = 0.0

    for aggr_name, data in aggregates.items():
        if aggr_name not in aggregate_info:
            logging.warning(f"Aggregate mapping data missing for '{aggr_name}'. Skipping.")
            continue
            
        total_allocated, total_used, total_available = data.values()
        aggr_size = aggregate_info[aggr_name]["size"]
        aggr_used = aggregate_info[aggr_name]["used"]
        aggr_avail = aggregate_info[aggr_name]["available"]
        aggr_percent_used = aggregate_info[aggr_name]["percent_used"]
        total_eff = aggregate_info[aggr_name]["total_eff"]
        data_red = aggregate_info[aggr_name]["data_red"]
        
        if aggr_size == 0:
            continue
            
        percent_allocated = (total_allocated / aggr_size) * 100
        percent_used = (total_used / aggr_size) * 100
        
        running_total_size_gb += aggr_size
        
        # Build structure for individual aggregate objects inside JSON (Including Tier mapping)
        aggr_json_map[aggr_name] = {
            "tier": "P80",
            "size_gb": round(aggr_size, 2),
            "used_gb": round(aggr_used, 2),
            "available_gb": round(aggr_avail, 2),
            "percent_used": round(aggr_percent_used, 2),
            "total_vol_allocated_gb": round(total_allocated, 2),
            "total_efficiency": total_eff,
            "data_reduction": data_red
        }
        
        summary_data.append([
            aggr_name, 
            f"{total_allocated:.2f}", f"{total_used:.2f}", f"{total_available:.2f}", 
            f"{aggr_size:.2f}", f"{aggr_used:.2f}", f"{aggr_avail:.2f}", 
            f"{percent_allocated:.2f}%", f"{percent_used:.2f}%", f"{aggr_percent_used:.2f}%",
            total_eff, data_red
        ])


    # Sort array before print and final mutations
    summary_data.sort(key=lambda x: x[0].split('_')[0])
    # --- Step 5: Construct and Write out the JSON Payload ---
    json_output_payload = {
        "environment": selected_env,
        "total_size_gb": round(running_total_size_gb, 2),
        "total_aggregates": len(summary_data),
        "aggregates": aggr_json_map,
        "volumes": parsed_volumes_list
    }
    
    date_str = datetime.now().strftime("%Y%m%d")
    json_filename = f"aggr-capacity-{selected_env}-{date_str}.json"
    try:
        with open(json_filename, "w") as json_file:
            json.dump(json_output_payload, json_file, indent=2)
        print(f"\nSuccessfully exported JSON structured profile payload to: {json_filename}")
    except IOError as e:
        logging.error(f"Failed to generate JSON configuration file: {e}")
    # Append footer summaries for local runtime grid output display only
    summary_data.append(["Total Aggregates", len(summary_data), "", "", "", "", "", "", "", "", "", ""])
    # Output formatted report grid directly to terminal
    print("\n" + tabulate(summary_data, headers=headers, tablefmt="grid"))
    # Write persistent CSV report file
    csv_filename = f"aggr-capacity-efficiency-{remote_host}-{date_str}.csv"
    try:
        with open(csv_filename, 'w', newline='') as csvfile:
            csvwriter = csv.writer(csvfile)
            csvwriter.writerow(headers)
            csvwriter.writerows(summary_data)
        print(f"Successfully exported capacity report with efficiency metrics to: {csv_filename}")
    except IOError as e:
        logging.error(f"Failed to generate CSV export file: {e}")
if __name__ == "__main__":
    remote_host = input("Enter the remote host IP or hostname: ")

    env_username = normalize_credential(os.environ.get("ONTAP_USER"))
    env_password = normalize_credential(os.environ.get("ONTAP_PASSWORD"))

    if env_username:
        username = env_username
        logging.info("Using username from ONTAP_USER environment variable.")
    else:
        username = input("Enter your username: ")

    if env_password:
        password = env_password
        logging.info("Using password from ONTAP_PASSWORD environment variable.")
    else:
        password = getpass.getpass("Enter your password: ")

    env_context = input("Enter environment identifier tag (e.g. prod, dev, stg): ") or "production"
    print(f"\nConnecting to {remote_host}...")
    
    print("Executing: volume show...")
    volume_output, volume_error = execute_ssh_command(
        remote_host, username, password, 
        "volume show -fields vserver,volume,aggregate,size,available"
    )
    if volume_error:
        logging.warning(f"Standard Error trace caught from Volume command:\n{volume_error}")

    print("Executing: aggr show...")
    aggregate_output, aggregate_error = execute_ssh_command(
        remote_host, username, password, 
        "aggr show -fields availsize,percent-used,size,usedsize"
    )
    if aggregate_error:
        logging.warning(f"Standard Error trace caught from Aggregate command:\n{aggregate_error}")

    print("Executing: storage aggregate show-efficiency...")
    efficiency_output, efficiency_error = execute_ssh_command(
        remote_host, username, password, 
        "storage aggregate show-efficiency"
    )
    if efficiency_error:
        logging.warning(f"Standard Error trace caught from Efficiency command:\n{efficiency_error}")

    print("Compiling cross-allocations and mapping data reduction statistics...\n")
    parse_aggregate_summary(volume_output, aggregate_output, efficiency_output, remote_host, env_context)