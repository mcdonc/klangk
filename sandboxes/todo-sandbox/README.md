# Sandbox test fixture

A tiny SQLite-backed todo app used to test `klangkc sandbox`. Not
used by CI or production — only for interactive evaluation.

## Usage

```bash
cd sandboxes/todo-sandbox
cp .env.example .env
klangkc sandbox -A
```

On first run, the setup script creates a couple of test todos and
marks one done. Inside the container:

```bash
python3 todo.py add "buy milk"
python3 todo.py ls
python3 todo.py done 3
python3 todo.py rm 2
```

The SQLite database lives at `~/.todo.db` inside the container.
