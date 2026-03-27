#!/bin/bash
# Run this script from your Mac terminal to create the GitHub repo and push.
# Prerequisites: gh CLI installed and authenticated (gh auth login)

set -e

REPO_NAME="maya-mcp"
DESCRIPTION="Image → 3D → Maya: Convert a 2D reference into a textured 3D mesh using Hunyuan3D-2 on a remote GPU, with a FastMCP server for LLM control of Autodesk Maya"

cd "$(dirname "$0")"

echo "Creating GitHub repository: $REPO_NAME"
gh repo create "$REPO_NAME" \
  --public \
  --description "$DESCRIPTION" \
  --source . \
  --remote origin \
  --push

echo ""
echo "Adding repository topics/tags..."
gh repo edit "$REPO_NAME" \
  --add-topic hunyuan3d \
  --add-topic maya \
  --add-topic 3d-generation \
  --add-topic ai \
  --add-topic python \
  --add-topic mcp \
  --add-topic texturing \
  --add-topic stable-diffusion \
  --add-topic blender-alternative \
  --add-topic autodesk-maya

echo ""
echo "Done! View your repo at:"
gh repo view --web
