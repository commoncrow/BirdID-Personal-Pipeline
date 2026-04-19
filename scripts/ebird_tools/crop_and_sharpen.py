import os
import sys
from PIL import Image, ImageFilter

def main():
    if len(sys.argv) < 2:
        print("Usage: python crop_and_sharpen.py <upload_ready_folder>")
        sys.exit(1)

    upload_ready = sys.argv[1]
    
    # Import SuperPicky relative to project root
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    sys.path.insert(0, project_root)
    from birdid.bird_identifier import get_yolo_detector

    detector = get_yolo_detector()

    if not detector:
        print("YOLO not available")
        sys.exit(1)

    if not os.path.exists(upload_ready):
        print(f"Folder not found: {upload_ready}")
        sys.exit(1)

    files = [f for f in os.listdir(upload_ready) if f.lower().endswith('.jpg')]

    for fname in files:
        path = os.path.join(upload_ready, fname)
        print(f"Processing {fname}...")
        
        try:
            img = Image.open(path)
            
            # Detect and crop using YOLO, padding ratio 0.8 provides aesthetic room around bird
            cropped_img, info = detector.detect_and_crop_bird(img, padding_ratio=0.8)
            
            if cropped_img:
                # Apply Unsharp Mask to sharpen
                sharpened_img = cropped_img.filter(ImageFilter.UnsharpMask(radius=2, percent=150, threshold=3))
                
                # Save it back
                sharpened_img.save(path, quality=95)
                print(f"  -> Cropped and sharpened! {info}")
            else:
                print(f"  -> No bird detected!")
        except Exception as e:
            print(f"  -> Failed: {e}")

if __name__ == "__main__":
    main()
