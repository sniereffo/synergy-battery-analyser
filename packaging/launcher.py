"""PyInstaller entry point for the standalone binaries.

The app package uses relative imports, so the binary starts from this tiny
script, which runs the same launcher as the `synergy-analyser` command.
"""

from synergy_analyser.cli import main

if __name__ == "__main__":
    main()
