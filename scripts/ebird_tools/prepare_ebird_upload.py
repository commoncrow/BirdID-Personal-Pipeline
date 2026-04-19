import os
import shutil
import sys

def main():
    if len(sys.argv) < 2:
        print("Usage: python prepare_ebird_upload.py <target_root_folder>")
        sys.exit(1)

    target_dir = sys.argv[1]
    star3 = os.path.join(target_dir, "3star_excellent", "Other_Birds")
    star2 = os.path.join(target_dir, "2star_good", "Other_Birds")
    out_dir = os.path.join(target_dir, "upload_ready")

    if not os.path.exists(out_dir):
        os.makedirs(out_dir)

    # Collect all species
    species_list = set()
    for d in [star3, star2]:
        if os.path.exists(d):
            folders = [f for f in os.listdir(d) if os.path.isdir(os.path.join(d, f))]
            species_list.update(folders)

    for s in species_list:
        photo = None
        
        # Check 3-star first
        p3 = os.path.join(star3, s)
        if os.path.exists(p3):
            for f in os.listdir(p3):
                if f.lower().endswith('.jpg'):
                    photo = os.path.join(p3, f)
                    break
        
        # If not found in 3-star, check 2-star
        if not photo:
            p2 = os.path.join(star2, s)
            if os.path.exists(p2):
                for f in os.listdir(p2):
                    if f.lower().endswith('.jpg'):
                        photo = os.path.join(p2, f)
                        break
        
        if photo:
            out_name = f'{s.replace(" ", "_")}.jpg'
            dest = os.path.join(out_dir, out_name)
            if not os.path.exists(dest):
                shutil.copy2(photo, dest)
            print(f'Found {s}: copied to {out_name}')
        else:
            print(f'No JPG found for {s}')

if __name__ == "__main__":
    main()
