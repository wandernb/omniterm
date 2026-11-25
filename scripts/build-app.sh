#!/bin/zsh
set -euo pipefail

project_dir="${0:A:h:h}"
fork_dir="$project_dir/Vendor/iTerm2"
final_app_dir="$project_dir/dist/OmniTerm.app"
# The bundle is assembled and signed in a staging copy, then synced into
# dist/OmniTerm.app in place. Deleting and recreating the bundle made macOS
# forget its Inbox widget extension, which removed the widget from
# Notification Center on every build.
app_dir="$project_dir/dist/.staging/OmniTerm.app"
configuration="${OMNITERM_CONFIGURATION:-Development}"
build_dir="${OMNITERM_BUILD_DIR:-}"
lsregister=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
verify_only="${OMNITERM_VERIFY_ONLY:-0}"
verify_stage=""
if [[ "$verify_only" != 0 && "$verify_only" != 1 ]]; then
    print -u2 "OMNITERM_VERIFY_ONLY must be 0 or 1."
    exit 2
fi
if [[ "$verify_only" == 1 ]]; then
    verify_parent="${OMNITERM_VERIFY_STAGING_ROOT:-$project_dir/dist/.staging}"
    if [[ "$verify_parent" != /* ]]; then
        print -u2 "OMNITERM_VERIFY_STAGING_ROOT must be an absolute path."
        exit 2
    fi
    mkdir -p "$verify_parent"
    verify_stage="$(mktemp -d "$verify_parent/verify.XXXXXX")"
    cleanup_verify_build() {
        # Xcode registers its product itself. Remove only this invocation's
        # temporary associations, including when the build fails midway.
        if [[ -n "$verify_stage" ]]; then
            local stage_real="${verify_stage:A}"
            local owned_app owned_real
            for owned_app in "$verify_stage/Products/$configuration/OmniTerm.app" "$verify_stage/OmniTerm.app"; do
                owned_real="${owned_app:A}"
                if [[ "$owned_real" == "$stage_real/"* && -d "$owned_real" ]]; then
                    "$lsregister" -u "$owned_real" 2>/dev/null || true
                fi
            done
            rm -rf -- "$verify_stage"
        fi
    }
    trap cleanup_verify_build EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    app_dir="$verify_stage/OmniTerm.app"
    # Never rebuild or remove an existing product in verification mode.
    # Xcode can still reuse the normal Development intermediates.
    build_dir="$verify_stage/Products"
    print -- "Verifying in temporary staging: $verify_stage"
fi

"$project_dir/scripts/iterm2-fork.sh" export

cd "$fork_dir"
if [[ "$configuration" == Deployment ]]; then
    cp plists/release-iTerm2.plist plists/iTerm2.plist
fi

# Xcode never removes files from an existing product, so start from an empty
# one. Compiled objects stay in Intermediates and are still reused.
if [[ "$verify_only" != 1 ]]; then
    if [[ -n "$build_dir" ]]; then
        previous_products=("$build_dir/$configuration/OmniTerm.app"(N))
    else
        previous_products=("$HOME"/Library/Developer/Xcode/DerivedData/iTerm2-*/Build/Products/$configuration/OmniTerm.app(N))
    fi
    if (( ${#previous_products} > 0 )); then
        rm -rf "${previous_products[@]}"
    fi
fi

if [[ "$verify_only" == 1 ]]; then
    SIGNED="" BUILD_DIR="$build_dir" tools/build.sh "$configuration"
elif [[ -n "$build_dir" ]]; then
    BUILD_DIR="$build_dir" tools/build.sh "$configuration"
else
    tools/build.sh "$configuration"
fi

if [[ -n "$build_dir" ]]; then
    products=("$build_dir/$configuration/OmniTerm.app"(Nom))
else
    products=("$HOME"/Library/Developer/Xcode/DerivedData/iTerm2-*/Build/Products/$configuration/OmniTerm.app(Nom))
fi
if (( ${#products} == 0 )); then
    print -u2 "Could not locate the native OmniTerm.app build product."
    exit 1
fi

rm -rf "$app_dir"
mkdir -p "${app_dir:h}"
ditto "$products[1]" "$app_dir"
# LaunchServices registers every app it sees and hands omniterm:// URLs (the
# widget's click) to whichever copy it prefers. The Xcode product has no
# helpers or widget and exits as a second copy, so it is removed once copied;
# normal builds delete it before building anyway, and Intermediates stay.
# Verification also unregisters its fresh product before removal; it never
# runs the normal global scan or registers a replacement for the active app.
for product in "${products[@]}"; do
    "$lsregister" -u "$product" 2>/dev/null || true
done
rm -rf "${products[@]}"
"$project_dir/scripts/check-bundle.sh" "$app_dir"
mkdir -p "$app_dir/Contents/Resources/Helpers"
install -m 0755 "$project_dir/helpers/omni" "$app_dir/Contents/Resources/Helpers/omni"
install -m 0755 "$project_dir/helpers/omni-runner" "$app_dir/Contents/Resources/Helpers/omni-runner"
install -m 0755 "$project_dir/helpers/omni-mcp" "$app_dir/Contents/Resources/Helpers/omni-mcp"
install -m 0755 "$project_dir/helpers/omni-knowledge" "$app_dir/Contents/Resources/Helpers/omni-knowledge"
for module in omni_knowledge.py omni_messages.py omni_embeddings.py omni_semantics.py omni_memory.py omni_json_stream.py omni_hippocampus.py omni_kb.py omni_episodes.py omni_redact.py omni_concepts.py omni_vectors.py omni_query.py omni_topics.py omni_storage.py omni_explain.py omni_summaries.py omni_blocks.py omni_unblocker.py omni_projects.py; do
    install -m 0644 "$project_dir/helpers/$module" "$app_dir/Contents/Resources/Helpers/$module"
done
install -m 0644 "$project_dir/helpers/omni_knowledge_schema.sql" "$app_dir/Contents/Resources/Helpers/omni_knowledge_schema.sql"
# Forge, OmniTerm's project layer, ships inside the app as a dependency
# (Contents/Resources/Forge): helpers/omni_projects.py runs an installed forge
# first and this copy otherwise. Forge stays its own repository; the source is
# $OMNI_FORGE_SRC, else the checkout behind `forge` on PATH. A release
# (Deployment) build fails without it; OMNI_BUNDLE_FORGE=0 leaves it out.
forge_src="${OMNI_FORGE_SRC:-}"
if [[ -z "$forge_src" ]] && command -v forge >/dev/null 2>&1; then
    forge_bin="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$(command -v forge)")"
    forge_src="$(dirname "$(dirname "$forge_bin")")"
fi
if [[ "${OMNI_BUNDLE_FORGE:-1}" != "0" ]]; then
    if [[ -n "$forge_src" && -f "$forge_src/bin/forge" && -d "$forge_src/forge_cli" ]]; then
        rm -rf "$app_dir/Contents/Resources/Forge"
        mkdir -p "$app_dir/Contents/Resources/Forge/bin"
        install -m 0755 "$forge_src/bin/forge" "$app_dir/Contents/Resources/Forge/bin/forge"
        for package in forge_cli adapters; do
            rsync -a --exclude '__pycache__' --exclude '*.pyc' "$forge_src/$package" "$app_dir/Contents/Resources/Forge/"
        done
        git -C "$forge_src" rev-parse --short HEAD > "$app_dir/Contents/Resources/Forge/VERSION" 2>/dev/null || true
        echo "Bundled forge $(cat "$app_dir/Contents/Resources/Forge/VERSION" 2>/dev/null)"
    elif [[ "$configuration" == "Deployment" ]]; then
        echo "error: a release bundles forge; set OMNI_FORGE_SRC to a forge checkout, or OMNI_BUNDLE_FORGE=0" >&2
        exit 1
    else
        echo "warning: forge not found; this build has no bundled forge" >&2
    fi
fi
install -m 0644 "$project_dir/assets/OmniTerm.icns" "$app_dir/Contents/Resources/OmniTerm.icns"
/usr/libexec/PlistBuddy -c 'Set :CFBundleIconFile OmniTerm.icns' "$app_dir/Contents/Info.plist"
/usr/libexec/PlistBuddy -c 'Delete :CFBundleIconName' "$app_dir/Contents/Info.plist" 2>/dev/null || true
# A menu-bar app from the first instant: launching never flashes a Dock icon
# or takes focus. OmniDockPresence makes it regular while a window is up.
/usr/libexec/PlistBuddy -c 'Delete :LSUIElement' "$app_dir/Contents/Info.plist" 2>/dev/null || true
/usr/libexec/PlistBuddy -c 'Add :LSUIElement bool true' "$app_dir/Contents/Info.plist"
# The Inbox widget is a WidgetKit extension compiled here, like the helpers,
# rather than an Xcode target in the fork.
widget_dir="$app_dir/Contents/PlugIns/OmniInboxWidget.appex"
mkdir -p "$widget_dir/Contents/MacOS"
install -m 0644 "$project_dir/widget/Info.plist" "$widget_dir/Contents/Info.plist"
# The SDK keys Xcode would add; WidgetKit reads them.
sdk_version="$(xcrun --show-sdk-version)"
sdk_build="$(xcrun --show-sdk-build-version)"
/usr/libexec/PlistBuddy \
    -c "Add :CFBundleSupportedPlatforms array" -c "Add :CFBundleSupportedPlatforms:0 string MacOSX" \
    -c "Add :DTPlatformName string macosx" -c "Add :DTSDKName string macosx$sdk_version" \
    -c "Add :DTPlatformVersion string $sdk_version" -c "Add :DTSDKBuild string $sdk_build" \
    -c "Add :DTPlatformBuild string $sdk_build" \
    "$widget_dir/Contents/Info.plist"
widget_architectures=("$(uname -m)")
[[ -n "${UNIVERSAL:-}" ]] && widget_architectures=(arm64 x86_64)
widget_slices=()
for architecture in "${widget_architectures[@]}"; do
    # App extensions start in NSExtensionMain, as Xcode links them; with the
    # default entry point macOS never loads the widget.
    xcrun swiftc -O -parse-as-library -application-extension \
        -enable-upcoming-feature ConciseMagicFile -file-prefix-map "$project_dir=OmniTerm" \
        -Xlinker -e -Xlinker _NSExtensionMain \
        -target "$architecture-apple-macos14" \
        "$project_dir/widget/OmniInboxWidget.swift" \
        -o "$widget_dir/Contents/MacOS/OmniInboxWidget-$architecture"
    widget_slices+=("$widget_dir/Contents/MacOS/OmniInboxWidget-$architecture")
done
lipo -create "${widget_slices[@]}" -output "$widget_dir/Contents/MacOS/OmniInboxWidget"
rm -f "${widget_slices[@]}"
if [[ "$verify_only" != 1 ]]; then
    mkdir -p "$HOME/.local/bin"
    ln -sfn "$final_app_dir/Contents/Resources/Helpers/omni" "$HOME/.local/bin/omni"
    ln -sfn "$final_app_dir/Contents/Resources/Helpers/omni-mcp" "$HOME/.local/bin/omni-mcp"
fi
if [[ "$verify_only" == 1 || -z "${OMNITERM_SKIP_SIGNING:-}" ]]; then
    # OMNITERM_SIGN_IDENTITY names a stable identity (a certificate hash or
    # name). A stable signature lets macOS keep the placed widget across
    # builds; ad-hoc ("-") changes with every build.
    if [[ "$verify_only" == 1 ]]; then
        identity="-"
    else
        identity="${OMNITERM_SIGN_IDENTITY:-}"
        local_keychain="$HOME/.omni/signing/omniterm-local.keychain-db"
        if [[ -f "$local_keychain" && -f "$HOME/.omni/signing/keychain-password" ]]; then
            # Locked after a restart; unlock it so codesign never prompts.
            security unlock-keychain -p "$(<"$HOME/.omni/signing/keychain-password")" "$local_keychain" 2>/dev/null || true
        fi
        if [[ -z "$identity" ]]; then
            # The local identity from scripts/local-signing-identity.sh, once its
            # keychain is in the search list; otherwise ad-hoc.
            identity="$(security find-identity -p codesigning 2>/dev/null \
                | awk '/"OmniTerm Local Signing"/ {print $2; exit}')"
            identity="${identity:--}"
        fi
    fi
    codesign --force --deep --sign "$identity" "$app_dir"
    # --deep signs nested code without entitlements, and a widget must be
    # sandboxed, so sign it again with its own, then reseal the app around it.
    codesign --force --sign "$identity" --entitlements "$project_dir/widget/OmniInboxWidget.entitlements" "$widget_dir"
    codesign --force --sign "$identity" "$app_dir"
    codesign --verify --deep --strict "$app_dir"
fi
if [[ "$verify_only" == 1 ]]; then
    print -- "Verified native OmniTerm, bundled helpers and widget with an ad-hoc signature. Temporary products are removed on exit."
    exit 0
fi
# Sync into place without ever removing the bundle or its widget extension.
mkdir -p "$final_app_dir"
rsync -a --delete "$app_dir/" "$final_app_dir/"
rm -rf "${app_dir:h}"
app_dir="$final_app_dir"
if [[ -z "${OMNITERM_SKIP_SIGNING:-}" ]]; then
    codesign --verify --deep --strict "$app_dir"
fi
# Widget clicks reach the app by bundle identifier, and LaunchServices prefers
# the highest version it knows, such as a kept release build. Unregister every
# other copy (files stay) so dist/ receives them.
while IFS= read -r copy; do
    [[ "$copy" == "$app_dir" ]] || "$lsregister" -u "$copy" 2>/dev/null || true
done < <(mdfind "kMDItemCFBundleIdentifier == 'com.omniterm.app'" 2>/dev/null)
"$lsregister" -f "$app_dir"

echo "$app_dir"
