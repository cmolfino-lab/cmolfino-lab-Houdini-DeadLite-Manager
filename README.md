# Houdini DeadLite Manager

**Houdini DeadLite Manager** is a highly lightweight, zero-installation, portable local render manager designed specifically for **SideFX Houdini** and **Solaris** workflows. 

*(Note: This tool is currently Windows-only.)*

It was built to solve a specific pain point: setting up official render managers like AWS Thinkbox Deadline or SideFX HQueue for a small home network or indie studio is often overkill. They require heavy database installations (like MongoDB), complex network configurations, and dedicated server software. 

Houdini DeadLite Manager provides a fast, drag-and-drop solution for small 2-to-3 machine network rendering without any of the headache.

## Download
You can download the latest pre-compiled, portable executables for Windows from the **[GitHub Releases](https://github.com/cmolfino-lab/cmolfino-lab-Houdini-DeadLite-Manager/releases)** page.


## Features
* **Zero-Installation:** Runs entirely from portable `.exe` files. No databases, no background services, no complex setup.
* **Houdini & Solaris Native:** Specifically designed to parse `.bat` files generated from Houdini containing `hython`, `hrender`, or `husk` commands.
* **Multi-Engine Support:** Works out-of-the-box with **Karma CPU**, **Karma XPU**, and **Redshift**.
* **Real-time Progress Tracking:** Intercepts `ALF_PROGRESS` statements natively from Houdini's command-line tools to provide live, dynamic progress bars in the UI.
* **Responsive Dashboard:** A clean, modern Web UI to track your Jobs, Tasks, and Worker statuses.

*(Note: Houdini DeadLite Manager is explicitly hard-coded to parse Houdini rendering prefixes. Out of the box, it will not work with other DCCs like Maya or Blender without modification.)*

## Architecture
Houdini DeadLite Manager uses a simple Manager/Worker architecture over standard HTTP requests:
* **`deadlite_manager.exe`**: Acts as the central server and hosts the dashboard UI on port `5000`. You run this on your main workstation.
* **`deadlite_worker.exe`**: Acts as the client. You run this on any rendering nodes on your network. It quietly polls the manager for `.bat` commands and executes them.

## Usage

1. **Download** the latest `deadlite_manager.exe` and `deadlite_worker.exe` from the [Releases](https://github.com/cmolfino-lab/cmolfino-lab-Houdini-DeadLite-Manager/releases) page.
2. **Launch `deadlite_manager.exe`** on your main workstation.
3. **Launch `deadlite_worker.exe`** on any machines you want to use for rendering. (Ensure their `config.json` points to the Manager's IP address).
4. **Open the Dashboard**: Navigate to `http://localhost:5000` in your web browser.
5. **Submit a Job**: Drag and drop a Houdini `.bat` file (see below) onto the dashboard to start rendering!

## Generating .bat Files (Houdini Shelf Tools)

To use Houdini DeadLite Manager, you need to generate chunked `.bat` files from Houdini. Below are two Python scripts you can paste into your **Houdini Shelf Tools** to automatically generate compatible files.

### 1. Solaris / USD (.bat Generator)
Use this tool when rendering a baked `.usd` file via `husk`. It allows you to explicitly force the GPU/CPU render delegate.

```python
import hou
import os
import math

def generate_husk_batch_script():
    hip_dir = hou.getenv('HIP')
    usd_file = hou.ui.selectFile(start_directory=hip_dir, title="Select Baked .usd", pattern="*.usd *.usda *.usdc", chooser_mode=hou.fileChooserMode.Read)
    if not usd_file: return
    usd_file = hou.expandString(usd_file)

    start_frame, end_frame = int(hou.playbar.playbackRange()[0]), int(hou.playbar.playbackRange()[1])
    frame_result = hou.ui.readMultiInput("Enter the Frame Range:", ("Start Frame", "End Frame"), title="Frame Range", buttons=("Next", "Cancel"), initial_contents=(str(start_frame), str(end_frame)))
    if frame_result[0] == 1: return
    
    f_start, f_end = int(frame_result[1][0]), int(frame_result[1][1])

    delegates = ["Karma CPU", "Karma XPU", "Redshift", "Default (Auto-detect)"]
    del_flags = ["-R Karma --engine cpu", "-R Karma --engine xpu", "-R Redshift", ""]
    
    del_choice = hou.ui.selectFromList(delegates, exclusive=True, title="Select Render Delegate")
    if not del_choice: return
        
    render_flag = del_flags[del_choice[0]]
    if render_flag: render_flag += " "

    batch_choice = hou.ui.displayMessage("How would you like to render?", buttons=("All in one batch", "Chunking", "Cancel"))
    if batch_choice == 2: return
        
    frames_per_batch = 0
    if batch_choice == 1:
        input_result = hou.ui.readInput("Frames per batch:", buttons=("OK", "Cancel"), initial_contents="10")
        if input_result[0] == 1: return
        frames_per_batch = int(input_result[1])

    bat_file = hou.ui.selectFile(start_directory=os.path.dirname(usd_file), title="Save Batch File", pattern="*.bat", chooser_mode=hou.fileChooserMode.Write)
    if not bat_file: return
    bat_file = hou.expandString(bat_file)
    if not bat_file.endswith('.bat'): bat_file += '.bat'

    h_bin = hou.getenv('HB').replace('\\', '/')
    usd_path = usd_file.replace('\\', '/')
    commands = [f'cd /d "{h_bin}"\n\n']
    total_frames = (f_end - f_start) + 1

    if batch_choice == 0 or frames_per_batch == 0:
        commands.append(f'husk {render_flag}-Va --make-output-path -f {f_start} -n {total_frames} "{usd_path}"\n')
    else:
        current_start = f_start
        while current_start <= f_end:
            frames_left = (f_end - current_start) + 1
            chunk_size = min(frames_per_batch, frames_left)
            commands.append(f'husk {render_flag}-Va --make-output-path -f {current_start} -n {chunk_size} "{usd_path}"\n')
            current_start += chunk_size

    commands.append('\n\npause\n')
    with open(bat_file, 'w') as f: f.writelines(commands)
    hou.ui.displayMessage(f"Saved to:\n{bat_file}", title="Success")

generate_husk_batch_script()
```

### 2. Standard / ROP (.bat Generator)
Use this tool for traditional Houdini rendering (e.g. Redshift out of the `/out` context) via `hython` and `hrender.py`.

> [!WARNING]
> **Important Note for Solaris users using `hython`**: If you are using this `hython` script to render a **USD Render ROP**, you **MUST** ensure the `Render All Frames with a Single Process` checkbox is TICKED on your USD Render ROP. Otherwise, Houdini will spin up a separate `husk` process for every single frame, which is extremely slow. For Solaris, it is highly recommended to use the `husk` script above instead!

```python
import hou
import os

def generate_hython_batch_script():
    hip_file = hou.hipFile.path()
    if hip_file == "untitled.hip" or not os.path.exists(hip_file):
        hou.ui.displayMessage("Please save your scene first.", severity=hou.severityType.Error)
        return
    hip_file = hip_file.replace('\\', '/')

    rop_path = hou.ui.selectNode(title="Select ROP Node", node_type_filter=hou.nodeTypeFilter.Rop)
    if not rop_path: return
    rop_node = hou.node(rop_path)
    
    try:
        start_frame, end_frame = int(rop_node.evalParm('f1')), int(rop_node.evalParm('f2'))
    except:
        start_frame, end_frame = int(hou.playbar.playbackRange()[0]), int(hou.playbar.playbackRange()[1])
        
    frame_result = hou.ui.readMultiInput("Enter the Frame Range:", ("Start Frame", "End Frame"), buttons=("Next", "Cancel"), initial_contents=(str(start_frame), str(end_frame)))
    if frame_result[0] == 1: return
    f_start, f_end = int(frame_result[1][0]), int(frame_result[1][1])

    batch_choice = hou.ui.displayMessage("How would you like to render?", buttons=("All in one batch", "Chunking", "Cancel"))
    if batch_choice == 2: return
        
    frames_per_batch = 0
    if batch_choice == 1:
        input_result = hou.ui.readInput("Frames per batch:", buttons=("OK", "Cancel"), initial_contents="10")
        if input_result[0] == 1: return
        frames_per_batch = int(input_result[1])

    bat_file = hou.ui.selectFile(start_directory=os.path.dirname(hip_file), title="Save Batch File", pattern="*.bat", chooser_mode=hou.fileChooserMode.Write)
    if not bat_file: return
    bat_file = hou.expandString(bat_file)
    if not bat_file.endswith('.bat'): bat_file += '.bat'

    h_bin = hou.getenv('HB').replace('\\', '/')
    commands = [f'cd /d "{h_bin}"\n\n']

    if batch_choice == 0 or frames_per_batch == 0:
        commands.append(f'hython hrender.py -e -f {f_start} {f_end} -d {rop_path} "{hip_file}"\n')
    else:
        current_start = f_start
        while current_start <= f_end:
            frames_left = (f_end - current_start) + 1
            chunk_size = min(frames_per_batch, frames_left)
            current_end = current_start + chunk_size - 1
            commands.append(f'hython hrender.py -e -f {current_start} {current_end} -d {rop_path} "{hip_file}"\n')
            current_start += chunk_size

    commands.append('\n\npause\n')
    with open(bat_file, 'w') as f: f.writelines(commands)
    hou.ui.displayMessage(f"Saved to:\n{bat_file}", title="Success")

generate_hython_batch_script()
```
