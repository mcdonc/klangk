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
    backend.exec = ''
      # Build frontend if not already built
      if [ ! -d frontend/build/web ]; then
        cd frontend && flutter pub get && flutter build web && rm -f build/web/flutter_service_worker.js && cd ..
      fi
      cd backend && uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8997
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
