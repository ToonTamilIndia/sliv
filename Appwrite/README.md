# Appwrite deployment

This directory contains the Appwrite Function entrypoint and the deployment-specific wrapper for the Flask proxy.

The function keeps the same request and response behavior as the local Python service:

- Appwrite receives the incoming request.
- The adapter converts the Appwrite request object into a Flask test-client request.
- The shared Flask application processes the route.
- The adapter copies the Flask response back into the Appwrite response object.

## Recommended structure

```
Appwrite/
├── main.py
├── requirements.txt
└── src/
    └── main.py
```

- [Appwrite/src/main.py](src/main.py) is the Appwrite function entrypoint.
- [Appwrite/main.py](main.py) remains as a compatibility entrypoint.
- [Appwrite/requirements.txt](requirements.txt) contains the Python dependencies required by the function runtime.

## How request handling works

### Incoming request mapping

The adapter reads the Appwrite request context and extracts:

- the HTTP method
- the request path
- the query string
- headers
- request body bytes

It then forwards that data into a Flask test client. This means the Appwrite deployment executes the same route logic as local development and does not need a separate code path for each endpoint.

### Response mapping

After Flask returns a response, the adapter:

- preserves the status code
- preserves response headers except `Content-Length`
- returns binary data when the Appwrite runtime supports it
- falls back to text or a JSON-like response object when needed

This keeps playlist responses, stream responses, and error responses consistent across deployments.

### Base URL handling

The adapter derives a public base URL for absolute playlist rewriting in this order:

1. `APPWRITE_FUNCTION_BASE_URL`
2. the request URL
3. forwarded host headers
4. `http://localhost/` as a final fallback

Set `APPWRITE_FUNCTION_BASE_URL` in production so rewritten playlists point to the correct public function domain.

## Environment variables

- `PROXY_SIGNING_KEY`: required in production to sign proxy tokens
- `APPWRITE_FUNCTION_BASE_URL`: recommended so rewritten playlists use the public Appwrite function URL

## Deployment steps

### 1. Install the Appwrite CLI

```bash
npm install -g appwrite-cli
appwrite login
```

### 2. Initialize the project

Run the initialization command from the repository root if the project is not already linked:

```bash
cd /home/toontamilindia/Projects/sliv
appwrite init project
```

### 3. Configure the function

In the Appwrite console or through the CLI, set:

- runtime: Python 3.12 or the newest supported Python runtime
- entrypoint: `Appwrite/src/main.py`
- build command: `pip install -r Appwrite/requirements.txt`

### 4. Add environment variables

Set the following values in the function settings:

- `PROXY_SIGNING_KEY`
- `APPWRITE_FUNCTION_BASE_URL`

### 5. Deploy

```bash
cd /home/toontamilindia/Projects/sliv
appwrite push function
```

## Example requests

```bash
curl -i "$APPWRITE_FUNCTION_BASE_URL/"
curl -i "$APPWRITE_FUNCTION_BASE_URL/playlist.m3u8?id=example"
curl -i "$APPWRITE_FUNCTION_BASE_URL/playlist.mpd?id=example&bandwidth=high"
```

## Logs and debugging

- Use the Appwrite console logs to inspect request failures and upstream errors.
- If playlists contain incorrect URLs, confirm that `APPWRITE_FUNCTION_BASE_URL` matches the deployed function URL.
- If tokenized URLs fail, confirm that `PROXY_SIGNING_KEY` is identical across every deployment environment.
- Use `curl -i` to inspect the returned status code and proxy headers.

## Common deployment mistakes

- Using an entrypoint outside the `Appwrite/` directory
- Forgetting to install dependencies with the function build command
- Omitting `PROXY_SIGNING_KEY` in production
- Deploying without a public base URL, which causes rewritten playlist URLs to point to the wrong origin
