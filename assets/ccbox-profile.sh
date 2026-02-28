# ccbox container shell profile — sourced by .bashrc
export PATH="$HOME/.local/bin:$PATH"
stty -ixon 2>/dev/null || true

# nvm (codex, node)
for _d in "$HOME"/.nvm/versions/node/*/bin; do
  [ -d "$_d" ] && PATH="$_d:$PATH"
done; unset _d

[ -n "$CCBOX_CWD" ] && cd "$CCBOX_CWD"
