{
  description = "Bandcamp library sync development environment";

  inputs = {
    flake-utils.url = "github:numtide/flake-utils";
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
  };

  outputs =
    {
      flake-utils,
      nixpkgs,
      self,
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        pkgs = import nixpkgs { inherit system; };

        python = pkgs.python313;
        pythonEnv = python.withPackages (
          ps: with ps; [
            beautifulsoup4
            pip
            playwright
            requests
            setuptools
          ]
        );
      in
      {
        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            pkgs.chromium
            pkgs.playwright-driver.browsers
          ];

          PLAYWRIGHT_BROWSERS_PATH = "${pkgs.playwright-driver.browsers}";
          PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD = "1";

          shellHook = ''
            export BANDCAMP_LIBRARY_SYNC_VENV_DISABLE=1
            export PYTHONPATH="$PWD/src''${PYTHONPATH:+:$PYTHONPATH}"

            cat <<'EOF'
bandcamp-library-sync dev shell

Python and project dependencies are available directly from Nix.
The repo's `src/` directory is added to `PYTHONPATH`, so you can run the CLI
without creating a venv or installing the package.

Suggested first steps:
  python -m bandcamp_library_sync.cli --help
  python -m bandcamp_library_sync.cli login

Playwright browsers are provided by Nix.
EOF
          '';
        };

        packages.default = pkgs.writeShellApplication {
          name = "bandcamp-library-sync-dev-help";
          text = ''
            echo "Enter the development shell with: nix develop"
          '';
        };
      }
    );
}
