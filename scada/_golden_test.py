"""The program's API answers on synthetic data are the same as the saved ones (tools/golden/*.json).
After a change that is meant to alter an answer: look at the differences, then  py tools\\replay_golden.py --save"""
import os, sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "tools"))
import replay_golden

sys.exit(replay_golden.main([]))
