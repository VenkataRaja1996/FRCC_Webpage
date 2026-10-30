# FRCC_Webpage
FRCC_Bulk Automation Project
## Installation and Setup

### 1. Project Folder Structure

Create a folder named `Bulk_Testing` in the D drive. Store the project files and Python scripts inside this folder.

**Project Location:**
```text
D:\Bulk_Testing
```

### 2. Install Required Python Packages

Open Command Prompt (CMD) and execute the following commands one by one to install the required dependencies.

```bash
python -m pip install flask

python -m pip install py7zr

python -m pip install rarfile
```

**Required Packages:**
- `Flask` – Used to run the web application and dashboard.
- `py7zr` – Used to handle 7z archive files.
- `rarfile` – Used to handle RAR archive files.

### 3. Launch the Web Dashboard

Follow these steps to open the web dashboard:

1. Press `Windows + R` on your keyboard.
2. Enter the following command in the Run dialog box.
3. Press `Enter`.

**Run Command:**

```cmd
cmd /k "cd /d D:\Bulk_Testing && python web_dashboard.py"
```

### 4. Access the Web Dashboard

Once the Python script starts successfully, the Flask application will launch.

Open your web browser and navigate to the local URL displayed in the CMD window (usually):

```text
http://127.0.0.1:5000
```

**Note:** Ensure that all project files, including `web_dashboard.py`, are available inside the `D:\Bulk_Testing` folder before launching the application.
