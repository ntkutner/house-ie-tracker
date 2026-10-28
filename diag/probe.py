import subprocess, sys, shutil, os
os.makedirs("diag/out", exist_ok=True)
shutil.copy("docs/data.json", "/tmp/old_data.json")
r = subprocess.run([sys.executable, "tracker.py", "--force", "--out", "diag/out"], capture_output=True, text=True)
print(r.stdout[-3000:]); print(r.stderr[-3000:])
r = subprocess.run([sys.executable, "late.py", "--force", "--out", "diag/out", "--cache", "cache/f6"], capture_output=True, text=True)
print(r.stdout[-3000:]); print(r.stderr[-3000:])
