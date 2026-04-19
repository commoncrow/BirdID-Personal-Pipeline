import os
import json
import shutil
import sys

def main():
    if len(sys.argv) < 2:
        print("Usage: python remove_chinese_names.py <dir1> [dir2 ...]")
        sys.exit(1)

    dirs_to_process = sys.argv[1:]

    for d in dirs_to_process:
        if not os.path.isdir(d):
            continue
        
        print(f"Processing directory: {d}")
        
        # rename directories
        for folder_name in os.listdir(d):
            folder_path = os.path.join(d, folder_name)
            if os.path.isdir(folder_path) and "_" in folder_name:
                cn_name, en_name = folder_name.split("_", 1)
                new_folder_path = os.path.join(d, en_name)
                
                if os.path.exists(new_folder_path):
                    for file_name in os.listdir(folder_path):
                        shutil.move(os.path.join(folder_path, file_name), os.path.join(new_folder_path, file_name))
                    os.rmdir(folder_path)
                    print(f"Merged {folder_name} into {en_name}")
                else:
                    os.rename(folder_path, new_folder_path)
                    print(f"Renamed {folder_name} to {en_name}")

        # update manifest if exists
        manifest_path = os.path.join(d, ".birdid_manifest.json")
        if os.path.exists(manifest_path):
            with open(manifest_path, "r", encoding="utf-8") as f:
                manifest = json.load(f)
                
            updated = False
            for move in manifest.get("moves", []):
                moved_to = move.get("moved_to", "")
                if moved_to:
                    dir_path = os.path.dirname(moved_to)
                    file_name = os.path.basename(moved_to)
                    folder_name = os.path.basename(dir_path)
                    if "_" in folder_name:
                        cn_name, en_name = folder_name.split("_", 1)
                        new_moved_to = os.path.join(os.path.dirname(dir_path), en_name, file_name)
                        move["moved_to"] = new_moved_to
                        updated = True
                        
            if updated:
                with open(manifest_path, "w", encoding="utf-8") as f:
                    json.dump(manifest, f, ensure_ascii=False, indent=2)
                print(f"Updated manifest in {d}")

if __name__ == "__main__":
    main()
