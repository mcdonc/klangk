{ pkgs, ... }: {
  languages.dart = {
    enable = true;
    package = pkgs.flutter;
  };
  languages.python = {
    enable = true;
    venv.enable = true;
    uv = {
      enable = true;
      sync.enable = true;
    };
    directory = "./backend";
  };

  packages = with pkgs; [
    docker-client
    nginx
  ];

  tasks = {
    "bark:flutter-build" = {
      exec = "flutterbuildweb";
      execIfModified = [
        "frontend/lib"
        "frontend/web"
        "frontend/pubspec.yaml"
        "frontend/pubspec.lock"
        "plugins/**/*.dart"
      ];
    };
    "bark:docker-build" = {
      exec = "dockerbuild";
      execIfModified = [
        "docker/Dockerfile"
        "docker/entrypoint.sh"
        "plugins/**/*.ts"
        "plugins/**/tools/**"
      ];
    };
  };

  processes = {
    backend = {
      exec = ''
        cd $DEVENV_ROOT/backend && uv run uvicorn backend.main:app --reload --host 0.0.0.0 --port 8997
      '';
      after = [
        "bark:flutter-build"
        "bark:docker-build"
      ];
    };
    nginx = {
      exec = ''
        mkdir -p $DEVENV_STATE/nginx
        cat > $DEVENV_STATE/nginx/nginx.conf << 'NGINX'
        daemon off;
        pid /tmp/nginx.pid;
        error_log stderr;
        events { worker_connections 64; }
        http {
          access_log /dev/stdout;
          client_body_temp_path /tmp/nginx_client_body;
          proxy_temp_path /tmp/nginx_proxy;
          fastcgi_temp_path /tmp/nginx_fastcgi;
          uwsgi_temp_path /tmp/nginx_uwsgi;
          scgi_temp_path /tmp/nginx_scgi;

          map $http_upgrade $connection_upgrade {
            default upgrade;
            "" close;
          }

          server {
            listen 8995;

            location /bark/ {
              proxy_pass http://127.0.0.1:8997/;
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;
              proxy_http_version 1.1;
              proxy_set_header Upgrade $http_upgrade;
              proxy_set_header Connection $connection_upgrade;
            }

            location / {
              proxy_pass http://127.0.0.1:8555/;
              proxy_set_header Host $host;
              proxy_set_header X-Real-IP $remote_addr;
              proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
              proxy_set_header X-Forwarded-Proto $scheme;
              proxy_http_version 1.1;
              proxy_set_header Upgrade $http_upgrade;
              proxy_set_header Connection $connection_upgrade;
            }
          }
        }
        NGINX
        nginx -c $DEVENV_STATE/nginx/nginx.conf
      '';
      after = [
        "bark:flutter-build"
        "bark:docker-build"
      ];
    };
  };

  env.SOURCE_DATE_EPOCH = "";
  dotenv.enable = true;

  scripts.flutterbuildweb.exec = ''
    cd $DEVENV_ROOT
    python3 scripts/gen_plugins.py
    cd frontend && flutter pub get && flutter build web --base-href=/bark/
    rm -f build/web/flutter_service_worker.js
  '';

  scripts.dockerbuild.exec = ''
    cd $DEVENV_ROOT
    # Collect plugin files into docker build context
    rm -rf docker/extensions docker/tools
    mkdir -p docker/extensions docker/tools
    for d in plugins/*/; do
      [ -d "$d" ] || continue
      name=$(basename "$d")
      # TypeScript extensions
      [ -f "$d/extension.ts" ] && cp "$d/extension.ts" "docker/extensions/$name.ts"
      # Server-side tools (any files in tools/ subdir)
      [ -d "$d/tools" ] && cp -r "$d/tools/"* docker/tools/ 2>/dev/null
    done
    # Remove old containers before rebuilding so they get recreated from the new image
    docker ps -a --filter ancestor=bark-pi -q | xargs -r docker rm -f
    docker build --platform linux/amd64 -t bark-pi docker/
  '';

  scripts.rebuild.exec = ''
    echo "Rebuilding Bark..."
    echo "==> Docker image"
    dockerbuild
    echo "==> Flutter web"
    flutterbuildweb
    echo "==> Done"
  '';

  scripts.update-plugins.exec = ''
    cd $DEVENV_ROOT
    python3 scripts/update_plugins.py "$@"
  '';

  enterShell = ''
    export BARK_DATA_DIR="''${DEVENV_STATE}/.bark"
    mkdir -p "$BARK_DATA_DIR"
    echo "Bark dev environment ready"
    echo "Run 'rebuild' to rebuild all sofware."
    echo "Run 'devenv processes up' to start backend + frontend"
  '';
}
