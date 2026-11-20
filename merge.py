from pathlib import Path
import shutil

def move_data_files(root_dir: str, target_folder_name: str = "collected_data") -> None:
    root_path = Path(root_dir).resolve()
    dest_path = root_path / target_folder_name
    dest_path.mkdir(parents=True, exist_ok=True)

    # Allowed extensions (case-insensitive)
    target_extensions = {".csv", ".sqlite", ".xlsx"}
    moved_count = 0

    # Find all files matching the target extensions across subfolders
    for filepath in root_path.rglob("*"):
        if filepath.is_file() and filepath.suffix.lower() in target_extensions:
            # Skip files already inside the destination directory
            if dest_path in filepath.parents:
                continue

            target_file = dest_path / filepath.name

            # Handle duplicate filenames across different subfolders
            if target_file.exists():
                prefix = filepath.parent.name
                target_file = dest_path / f"{prefix}_{filepath.name}"
                
                counter = 1
                while target_file.exists():
                    target_file = dest_path / f"{prefix}_{counter}_{filepath.name}"
                    counter += 1

            try:
                shutil.move(str(filepath), str(target_file))
                print(f"Moved: {filepath.relative_to(root_path)} -> {target_file.name}")
                moved_count += 1
            except Exception as e:
                print(f"Failed to move {filepath}: {e}")

    print(f"\nDone! Successfully moved {moved_count} file(s) to: {dest_path}")

if __name__ == "__main__":
    # Replace with your folder path
    folder_path = r"D:\train\data"
    move_data_files(root_dir=folder_path)