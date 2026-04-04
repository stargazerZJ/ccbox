# ccbox container shell profile — sourced by .bashrc
export IS_SANDBOX=1
export PATH="$HOME/.local/bin:$PATH"
stty -ixon 2>/dev/null || true

# nvm (codex, node)
for _d in "$HOME"/.nvm/versions/node/*/bin; do
  [ -d "$_d" ] && PATH="$_d:$PATH"
done; unset _d

[ -n "$CCBOX_CWD" ] && cd "$CCBOX_CWD"

# Explicitly unset whitelisted env vars that were not set on the host,
# so the container shell doesn't inherit stale values from the login env.
if [ -n "$CCBOX_UNSET_VARS" ]; then
  _IFS_SAVE="$IFS"; IFS=','
  for _var in $CCBOX_UNSET_VARS; do
    unset "$_var"
  done
  IFS="$_IFS_SAVE"; unset _IFS_SAVE _var CCBOX_UNSET_VARS
fi
