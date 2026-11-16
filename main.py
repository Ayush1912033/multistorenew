from pathlib import Path
import tkinter as tk
from src.multi_store_gui import MultiStoreApp


def main():
    base_dir = Path(__file__).resolve().parent
    root = tk.Tk()
    MultiStoreApp(root, base_dir)
    root.mainloop()


if __name__ == '__main__':
    main()
