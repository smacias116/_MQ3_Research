import subprocess
import time
import csv
import sys
from datetime import datetime
import pandas as pd

target_device = "192.168.0.240:5555"
session_timestamp = int(time.time())
raw_filename = f"metrics_log_{session_timestamp}.csv"
processed_filename = f"metrics_log_power_{session_timestamp}.csv"

print(f"Starting Quest 3 Logger...")
print(f"Target Device: {target_device}")
print(f"Raw telemetry file: {raw_filename}\n")

# --- PHASE 1: LIVE RECORDING ---
with open(raw_filename, mode='w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow([
        "Timestamp", 
        "Level (%)", 
        "Voltage (mV)", 
        "Temp (C)", 
        "Charge Counter (uAh)"
    ])

    try:
        while True:
            output = subprocess.check_output(
                ["adb", "-s", target_device, "shell", "dumpsys", "battery"]
            ).decode('utf-8', errors='ignore')
            lines = output.splitlines()

            data = {}
            for line in lines:
                if ":" in line:
                    key, val = line.split(":", 1)
                    data[key.strip()] = val.strip()

            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            level = data.get("level", "N/A")
            voltage = int(data.get("voltage", 0))
            temp = float(data.get("temperature", 0)) / 10.0 if "temperature" in data else "N/A"
            charge_counter = int(data.get("Charge counter", 0))

            writer.writerow([timestamp, level, voltage, temp, charge_counter])
            f.flush()

            print(f"[{timestamp}] Level: {level}% | Volts: {voltage}mV | Temp: {temp}C | Charge Counter: {charge_counter} uAh")
            time.sleep(1)

    except KeyboardInterrupt:
        print("\n\nRecording stopped by user.")
        print(f"Raw data successfully saved to: {raw_filename}")

# --- PHASE 2: AUTOMATIC POST-PROCESSING ---
print("\nStarting post-processing time-alignment...")

try:
    df = pd.read_csv(raw_filename)

    if len(df) < 2:
        print("Not enough data logged to perform post-processing.")
        sys.exit()

    # Identify rows where the Charge Counter updates
    df['charge_change'] = df['Charge Counter (uAh)'].diff() != 0
    change_indices = df[df['charge_change']].index.tolist()

    if 0 not in change_indices:
        change_indices.insert(0, 0)
    if (len(df) - 1) not in change_indices:
        change_indices.append(len(df) - 1)

    df['Calculated Current (mA)'] = 0.0
    df['Calculated Power (mW)'] = 0.0

    for i in range(1, len(change_indices)):
        start_idx = change_indices[i - 1]
        end_idx = change_indices[i]

        delta_charge = df.loc[start_idx, 'Charge Counter (uAh)'] - df.loc[end_idx, 'Charge Counter (uAh)']
        delta_t = end_idx - start_idx  # 1 row = 1 second

        if delta_t >= 10 and delta_charge > 0:
            avg_current = round((delta_charge * 3600 / delta_t) / 1000, 2)

            df.loc[start_idx:end_idx - 1, 'Calculated Current (mA)'] = avg_current

            df.loc[start_idx:end_idx - 1, 'Calculated Power (mW)'] = (
                (df.loc[start_idx:end_idx - 1, 'Voltage (mV)'] / 1000.0) * avg_current
            ).round(2)

    df.drop(columns=['charge_change'], inplace=True)

    df.to_csv(processed_filename, index=False)
    print(f"Post-processing complete! Perfectly aligned data saved to: {processed_filename}")

except Exception as e:
    print(f"An error occurred during post-processing: {e}")