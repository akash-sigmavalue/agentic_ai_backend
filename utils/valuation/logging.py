import os
import json
from datetime import datetime

class RunLogger:
    def __init__(self, project_name: str):
        self.project_name = project_name.replace(" ", "_").replace("/", "_")
        self.timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.run_dir = os.path.join("run_logs", f"{self.timestamp}_{self.project_name}")
        
    def _ensure_dir(self, subfolder: str = ""):
        path = os.path.join(self.run_dir, subfolder)
        os.makedirs(path, exist_ok=True)
        return path

    def save_step(self, module: str, step_name: str, data: any):
        """
        Saves data to a JSON file within the run directory.
        module: e.g. 'comparable_agent' or 'listing_search'
        step_name: e.g. 'pass_1_raw'
        """
        target_dir = self._ensure_dir(module)
        file_path = os.path.join(target_dir, f"{step_name}.json")
        
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            # We don't want logger failure to crash the pipeline
            print(f"Error saving log step {step_name}: {e}")

    def save_text(self, module: str, step_name: str, text: str):
        """Saves raw text (like scraped HTML/text) to a file."""
        target_dir = self._ensure_dir(module)
        file_path = os.path.join(target_dir, f"{step_name}.txt")
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            print(f"Error saving log text {step_name}: {e}")

    def save_raw(self, module: str, filename: str, content: str):
        """Saves raw content with a specific filename/extension."""
        target_dir = self._ensure_dir(module)
        file_path = os.path.join(target_dir, filename)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"Error saving raw file {filename}: {e}")
