# flyme_album_sync

Download and metadata-sync Flyme Photos albums from a browser "Copy as cURL" request.

## Usage

Save the copied album/list cURL command as `flyme_album.curl`, then run:

```bash
./flyme_album_sync.py \
  --curl-file ./flyme_album.curl \
  --out /Users/chaoyang/Downloads/album_186604 \
  --download
```

For existing downloaded files, omit `--download`:

```bash
./flyme_album_sync.py \
  --curl-file ./flyme_album.curl \
  --out /Users/chaoyang/Downloads/album_186604
```

Use `--dry-run` to preview operations.

