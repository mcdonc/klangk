# Files

The Files tab provides a file browser for your workspace container.
Browse directories, view file contents, and upload or download files —
all without using the terminal.

Click the **Files** tab in the workspace to open.

[![File browser showing workspace files](../assets/files/01-file-browser.png)](../assets/files/01-file-browser.png)

## Browsing

The file browser shows the contents of the workspace's `/home/work`
directory. Click a directory to enter it, or click a file to preview
its contents.

- **Path bar** at the top shows the current directory with clickable
  breadcrumbs. Click `/` to return to the root, or use the up-arrow
  button to go up one level.
- **File sizes** are shown next to each entry.
- **Auto-refresh** — the file list refreshes automatically when Pi
  creates, edits, or deletes files, and when you switch to the Files
  tab.

## Uploading Files

Drag and drop files or folders onto the file browser to upload them.
Uploads go into the currently viewed directory (not always root).

- **Folder upload** preserves directory structure
- **Progress indicator** shows upload status
- **Duplicate detection** — blocks upload if a file or folder with
  the same name already exists
- Maximum upload size: 500 MB (configurable via `KLANGK_IMPORT_MAX_SIZE`)

## Downloading

Right-click a file or folder to open the context menu:

- **Download** — downloads the file directly
- **Download folder** — downloads the folder as a `.zip` archive
  (zipped on the fly by the backend)

## File Preview

Click a file to view its contents in a read-only viewer with syntax
highlighting (JetBrains Mono font). Toggle between rendered **View**
and **Raw** modes using the buttons at the top right.

[![File preview showing rendered Markdown](../assets/files/02-file-preview.png)](../assets/files/02-file-preview.png)

Supported previews include:

- Source code files with syntax highlighting
- Text files
- Images
- PDF documents

## Context Menu

Right-click any file or folder for these actions:

- **Download** — download the file or folder
- **Rename** — rename with an inline dialog
- **Delete** — delete with confirmation prompt
