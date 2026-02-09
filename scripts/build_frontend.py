Import("env")
import subprocess
import os

frontend_dir = os.path.join(env.subst("$PROJECT_DIR"), "frontend")
assets_header = os.path.join(env.subst("$PROJECT_DIR"), "src", "web_assets.h")

# Skip if web_assets.h already exists and is newer than all frontend sources
needs_rebuild = not os.path.exists(assets_header)

if not needs_rebuild:
    header_mtime = os.path.getmtime(assets_header)
    src_dir = os.path.join(frontend_dir, "src")
    for root, dirs, files in os.walk(src_dir):
        for f in files:
            if os.path.getmtime(os.path.join(root, f)) > header_mtime:
                needs_rebuild = True
                break
    # Also check index.html
    index_html = os.path.join(frontend_dir, "index.html")
    if os.path.exists(index_html) and os.path.getmtime(index_html) > header_mtime:
        needs_rebuild = True

if needs_rebuild:
    subprocess.check_call(["npm", "i"], cwd=frontend_dir)
    print("Building frontend...")
    subprocess.check_call(["npm", "run", "build"], cwd=frontend_dir)
else:
    print("Frontend assets up to date, skipping build")
