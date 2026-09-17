# Legacy VisionEdge Tests

These standalone tests belong to the original multi-page VisionEdge base.
They are retained for reference while `visionedge_server.py` still imports
`server.py` and `vision_core.py`.

They are not part of the maintained APL command:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```

Run an individual legacy script explicitly from the project root when its
older feature area is being changed.
