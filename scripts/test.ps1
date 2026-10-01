# Run the test suite locally (needs: pip install -r requirements.txt)
$env:PYTHONPATH = "src;tests"
python -m unittest discover -s tests -v
