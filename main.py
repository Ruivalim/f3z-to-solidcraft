"""
Name: f3z-to-solidcraft

Description: Recreate a Fusion .f3z/.f3d design in SolidCraft.

Disclainer:
This project has no affiliation with Autodesk, Inc. or Fusion 360. It is a personal project and is not endorsed by Autodesk, Inc. 
Secondly, this project is not affiliated with SolidCraft (https://github.com/bherbruck/solidcraft). It is a personal project and is not currently endorsed by SolidCraft developers.

Credits:
Using https://github.com/AlexSabaka/ezf3d to read Fusion 360 files

AI Disclouser:
The main idea and implementation was done by me, but I used claude to help with some of the code and to help me understand the Fusion 360 file formats.

Usage: 
main.py SOURCE [--save [PATH]] [--open] [--force] [--exact-on-mismatch]

"""

from __future__ import annotations

import sys

from utils.converter import main

if __name__ == "__main__":
    sys.exit(main())
