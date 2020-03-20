#!/usr/bin/env python3

import os.path

from setuptools import find_packages, setup

with open(os.path.join(os.path.dirname(__file__), "README.rst"), encoding="utf-8") as f:
    long_description = f.read()

setup(
    name="overview-plugin-workqueue",
    version="1.0.0",
    description="In-process work queue for use in Overview plugins",
    long_description=long_description,
    long_description_content_type="text/x-rst",
    author="Adam Hooper",
    author_email="adam@adamhooper.com",
    url="https://github.com/overview/py-plugin-workqueue",
    packages=find_packages("src"),
    package_dir={"": "src"},
    classifiers=[
        "Programming Language :: Python :: 3",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
)
