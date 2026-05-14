{ pkgs, ... }: {
  languages.dart = {
    enable = true;
    package = pkgs.flutter;
  };
  languages.python = {
    enable = true;
    uv = {
      enable = true;
      sync.enable = true;
    };
    directory = "./backend";
  };

  packages = with pkgs; [
    docker-client
  ];

  processes = {
    backend.exec = "cd backend && uv run uvicorn backend.main:app --reload --port 8996";
    frontend.exec = ''
      cd frontend
      if [ ! -d build/web ]; then
        flutter pub get && flutter build web
      fi
      # Remove service worker to prevent aggressive caching during dev
      rm -f build/web/flutter_service_worker.js
      python3 -m http.server 8997 --bind 0.0.0.0 --directory build/web
    '';
  };

  dotenv.enable = true;

  scripts.rebuild.exec = ''
    echo "Rebuilding Bark..."
    echo "==> Docker image"
    docker build --platform linux/amd64 -t bark-pi docker/
    echo "==> Flutter web"
    cd frontend && flutter pub get && flutter build web
    rm -f build/web/flutter_service_worker.js
    echo "==> Done"
  '';

  enterShell = ''
    echo "Bark dev environment ready"
    export BARK_DATA_DIR="''${DEVENV_STATE}/.bark"
    mkdir -p "$BARK_DATA_DIR"

    # Build Pi agent Docker image if not already built or if Dockerfile changed
    if ! docker image inspect bark-pi >/dev/null 2>&1 || \
       [ docker/Dockerfile -nt "$(docker image inspect bark-pi --format='{{.Created}}' 2>/dev/null || echo '0')" ]; then
      echo "Building bark-pi Docker image..."
      docker build --platform linux/amd64 -t bark-pi docker/
    else
      echo "bark-pi Docker image already up to date"
    fi

    echo "Run 'devenv processes up' to start backend + frontend"
  '';
}
