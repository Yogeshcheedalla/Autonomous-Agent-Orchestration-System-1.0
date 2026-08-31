"""
Pytest configuration for aura/backend tests.
Adds the backend directory to sys.path so agent_modules is importable.
"""
import sys
import os

# Add aura/backend to path so agent_modules can be imported
sys.path.insert(0, os.path.dirname(__file__))
