"""Run editable modeling source with the selected Python interpreter."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent/'src'))
if __name__=='__main__':
    from markmodel.cli import main
    raise SystemExit(main())
