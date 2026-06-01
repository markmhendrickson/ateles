#!/usr/bin/env python3
"""
Interactive PDF Field Calibrator

GUI tool to click on form fields and automatically generate positions JSON.

Usage:
    python pdf-interactive-calibrator.py --template form.pdf --data data.json --output positions.json
"""

import argparse
import json
import sys
from pathlib import Path

try:
    import tkinter as tk

    from pdf2image import convert_from_path
    from PIL import Image, ImageTk
except ImportError as e:
    print("Error: Missing required library. Install with:")
    print("  pip install pdf2image pillow")
    print("  brew install poppler  # macOS")
    print(f"Missing: {e}")
    sys.exit(1)


class PDFCalibrator:
    def __init__(self, pdf_path, data_path, output_path):
        self.pdf_path = pdf_path
        self.data_path = data_path
        self.output_path = output_path

        # Load data to know which fields to calibrate
        with open(data_path) as f:
            self.data = json.load(f)

        # Convert PDF to image
        print("Converting PDF to image...")
        images = convert_from_path(pdf_path, first_page=1, last_page=1, dpi=150)
        if not images:
            print("Error: Could not convert PDF")
            sys.exit(1)

        self.image = images[0]
        self.page_width_pts = 595.32  # A4 width in points
        self.page_height_pts = 841.92  # A4 height in points

        # Track positions
        self.positions = {}
        self.current_field_index = 0
        self.fields = [
            k
            for k in self.data.keys()
            if not isinstance(self.data[k], bool) or self.data[k]
        ]

        # Setup GUI
        self.root = tk.Tk()
        self.root.title("PDF Field Calibrator")

        # Display image
        self.display_image()

        # Instructions
        self.instruction_label = tk.Label(
            self.root,
            text=f"Click on field: {self.fields[self.current_field_index]}",
            font=("Arial", 14, "bold"),
        )
        self.instruction_label.pack(pady=10)

        # Bind click
        self.canvas.bind("<Button-1>", self.on_click)

        # Navigation buttons
        button_frame = tk.Frame(self.root)
        tk.Button(button_frame, text="Skip", command=self.skip_field).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(button_frame, text="Undo", command=self.undo_last).pack(
            side=tk.LEFT, padx=5
        )
        tk.Button(button_frame, text="Save & Exit", command=self.save_and_exit).pack(
            side=tk.LEFT, padx=5
        )
        button_frame.pack(pady=10)

    def display_image(self):
        # Resize for display (max 1200px wide)
        display_width = min(1200, self.image.width)
        display_height = int(self.image.height * (display_width / self.image.width))
        display_img = self.image.resize(
            (display_width, display_height), Image.Resampling.LANCZOS
        )

        self.photo = ImageTk.PhotoImage(display_img)
        self.canvas = tk.Canvas(self.root, width=display_width, height=display_height)
        self.canvas.create_image(0, 0, anchor=tk.NW, image=self.photo)
        self.canvas.pack()

        # Store scale factors
        self.scale_x = self.page_width_pts / display_width
        self.scale_y = self.page_height_pts / display_height

    def on_click(self, event):
        # Convert display coordinates to PDF points
        pdf_x = event.x * self.scale_x
        pdf_y = (self.canvas.winfo_height() - event.y) * self.scale_y  # Flip Y

        field_name = self.fields[self.current_field_index]
        self.positions[field_name] = [pdf_x, pdf_y, 0]

        # Draw marker
        self.canvas.create_oval(
            event.x - 5,
            event.y - 5,
            event.x + 5,
            event.y + 5,
            fill="red",
            outline="red",
            width=2,
        )
        self.canvas.create_text(
            event.x, event.y - 15, text=field_name[:20], fill="red", font=("Arial", 8)
        )

        # Move to next field
        self.current_field_index += 1
        if self.current_field_index < len(self.fields):
            self.instruction_label.config(
                text=f"Click on field: {self.fields[self.current_field_index]}"
            )
        else:
            self.instruction_label.config(
                text="All fields calibrated! Click 'Save & Exit'"
            )

    def skip_field(self):
        self.current_field_index += 1
        if self.current_field_index < len(self.fields):
            self.instruction_label.config(
                text=f"Click on field: {self.fields[self.current_field_index]}"
            )
        else:
            self.instruction_label.config(
                text="All fields calibrated! Click 'Save & Exit'"
            )

    def undo_last(self):
        if self.current_field_index > 0:
            self.current_field_index -= 1
            field_name = self.fields[self.current_field_index]
            if field_name in self.positions:
                del self.positions[field_name]
            self.instruction_label.config(
                text=f"Click on field: {self.fields[self.current_field_index]}"
            )
            # Redraw canvas (remove markers)
            self.display_image()
            # Redraw existing markers
            for name, (x, y, page) in self.positions.items():
                display_x = x / self.scale_x
                display_y = self.canvas.winfo_height() - (y / self.scale_y)
                self.canvas.create_oval(
                    display_x - 5,
                    display_y - 5,
                    display_x + 5,
                    display_y + 5,
                    fill="green",
                    outline="green",
                    width=2,
                )

    def save_and_exit(self):
        with open(self.output_path, "w") as f:
            json.dump(self.positions, f, indent=2)
        print(f"\n✓ Saved {len(self.positions)} field positions to {self.output_path}")
        self.root.quit()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    parser = argparse.ArgumentParser(
        description="Interactive GUI tool to calibrate PDF form field positions"
    )

    parser.add_argument("--template", required=True, help="PDF template file")
    parser.add_argument("--data", required=True, help="JSON data file with field names")
    parser.add_argument("--output", required=True, help="Output positions JSON file")

    args = parser.parse_args()

    if not Path(args.template).exists():
        print(f"Error: Template file not found: {args.template}")
        sys.exit(1)

    if not Path(args.data).exists():
        print(f"Error: Data file not found: {args.data}")
        sys.exit(1)

    calibrator = PDFCalibrator(args.template, args.data, args.output)
    calibrator.run()


if __name__ == "__main__":
    main()
