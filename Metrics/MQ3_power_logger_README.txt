*********************************************************
***EXECUTION OF MQ3_power_logger.py FOR METRIC LOGGING***
*********************************************************

--> The MQ3_power_logger.py works on the same folder as ADB package:
    My laptop (stored in two directories, working in the first):
	C:\Users\user\OneDrive - Loyola University New Orleans\FALL_2026\_MQ3_Research\Metrics
	C:\platform-tools\platform-tools

EXECUTING FILE WITH POWER SHELL:

--> (1) Go to the directory. For my laptop:
	cd "C:\Users\user\OneDrive - Loyola University New Orleans\FALL_2026\_MQ3_Research\Metrics"

--> (2) Check connectivity:
	.\adb.exe devices 	
	    If failing: 
		- Review WiFi/Bluetooth connections,
		- Cehck in Meta Quest Developer Hub: Is "ADB on WiFi" enabled?,
		- USB is disconnected
	NOTE: To see the real-time raw battery data the device is returning to ADB right now:
	.\adb.exe -s 192.168.0.240:5555 shell dumpsys batter

--> (3) Run the script:
	python MQ3_power_logger.py

    NOTES:
    - For MQ3_power_logger_V1.py:
        The logger will start tracking data, but it includes a "manual check" to control when de video is played/stopped.
	Press any key on your laptop when the video starts,
        Press any key on your laptop when the video finishes,
    - If want to check .csv file while running: open it with VSC or notepad (NOT Excel --> will cause error)

--> (4) Stop recording:	
	Ctrl + C
   
    NOTES:
    - Two files are generated:
        metrics_log_1790776139.csv (raw data from ADB)
	metrics_log_power_1790774700.csv (post-processed including Calculated Data)


*********************************************************
***CSV FILE DATA*** (for version MQ3_power_logger_V1.py)
*********************************************************

- Identification and time
id_line: 	row number of the sample (1, 2, 3...), one per reading, about once per second.
Timestamp: 	date and time of the reading (YYYY-MM-DD HH:MM:SS).
Elapsed (s): 	seconds since the recording started, measured precisely. The calculations use this instead of the row count.

- Battery
Level (%):		battery percentage at that moment.
Voltage (mV): 		battery voltage in millivolts.
Temp (C): 		battery temperature in °C.
Charge Counter (uAh): 	charge remaining in the battery, in microamp-hours. It goes down as the battery discharges.
Status: 		charging state as a number: 1 Unknown, 2 Charging, 3 Discharging, 4 Not charging, 5 Full.
Health: 		battery health as a number: 1 Unknown, 2 Good, 3 Overheat, 4 Dead, 5 Over voltage, 6 Unspecified failure, 7 Cold.
Present: 		true if the battery is detected.
Scale: 			the maximum value of the level (normally 100).
AC Powered, 
USB Powered, 
Wireless Powered, 
Dock Powered: 		true or false, depending on whether the headset is receiving power from that source.
Max Charging Current (mA),
Max Charging Voltage (mV): the maximum the connected charger can supply, as reported by Android.
Weak Charger: 		whether the charger is flagged as weak. It shows N/A if your firmware doesn't report it.
Technology: 		battery type (for example Li-ion).
Extra columns: 		If your firmware reports other battery fields, they appear here automatically under their own names.
Status (text): 		Status in words (Charging, Discharging...).
Health (text): 		Health in words (Good, Overheat...).

- Volume and brightness
Volume (index): 	current media volume step.
Volume Max: 		highest volume step.
Volume (%): 		volume as a percentage of the maximum.
Brightness (0-255): 	the screen brightness setting.
Brightness (%): 	the same value as a percentage.
Brightness Mode: 	Manual or Auto.

- App and video
Foreground App: 	package name of the app currently in focus.
Media App: 		package name of the media player the script found (YouTube VR in your case).
Video Title: 		the title, only if the app publishes it. YouTube VR doesn't, so this is normally N/A.
Title Source: 		auto if a title was found, otherwise N/A.
Playback State: 	Playing (audio active) or Not playing, based on whether audio is being output.
Video Clock: 		your manual clock state: Idle (before the first key), Running or Stopped.
Video Session: 		counts your videos: 0 before the first key press, 1 for the first video, 2 for the second, and so on.
Video Position (s),
Video Position (mm:ss): time since you pressed the key at the start of the video. They show N/A when the clock isn't running.
Position Source: 	manual clock, or auto if the app ever publishes its own position.

- Processed file only [These four columns sit right after Charge Counter (uAh)]:
Calculated Current (mA):	average current the headset was drawing. It's calculated from how much the charge counter dropped between its changes, and it stays at 0 while charging or when the window is shorter than 10 seconds.
Calculated Power (mW): 		power drawn, equal to voltage × current.
Accumulated Power (mWh): 	running total of energy used since the start of the recording, up to and including that row.
Accumulated Current (mAh): 	running total of charge used since the start, calculated the same way.