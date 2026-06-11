# File Viewer

- Directory tree with file sizes
- Click to view file contents (16pt JetBrains Mono, left-aligned)
- Auto-refresh when Pi writes/edits files or runs file-creating/deleting bash commands
- Auto-refresh when switching to the Files tab (refreshes in-place, preserving current directory)
- Drag-and-drop upload for files and folders (preserves directory structure, progress indicator)
- Uploads go into the currently viewed directory (not always root)
- Duplicate detection: blocks upload if a file or folder with the same name already exists
- Right-click context menu on files and folders: Download, Rename (with dialog), and Delete (with confirmation)
- Download files directly; download folders as .zip (zipped on the fly by the backend)
- Path bar with ellipsis overflow, clickable `/` root link, and up-arrow navigation button
- nginx `client_max_body_size 500m` for large file uploads
- nginx `sub_filter` rewrites `<base href>` for subpath hosting (`/klangk/`)
