#!/bin/sh
# Відновлення робочого середовища на сервері «ai» (DL165 G7 + RX 580) після перевстановлення Ubuntu.
# Запускати від користувача kpl з sudo без пароля:  sh provision-ai-server.sh
# Ідемпотентний: повторний запуск нічого не ламає.
set -eu

echo "== пакети"
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
  build-essential cmake git ffmpeg \
  mesa-vulkan-drivers libvulkan-dev vulkan-tools glslc spirv-headers spirv-tools glslang-tools \
  smartmontools lm-sensors ipmitool

echo "== доступ до GPU без sudo (Vulkan/RADV потребує групи render)"
sudo usermod -aG render,video "$USER"

echo "== пам'ять карти не має сидіти на 300 МГц без монітора: високий профіль живлення після кожного завантаження"
sudo tee /etc/udev/rules.d/90-amdgpu-perf.rules >/dev/null <<'EOF'
KERNEL=="card[0-9]*", SUBSYSTEM=="drm", DRIVERS=="amdgpu", ATTR{device/power_dpm_force_performance_level}="high"
EOF
sudo udevadm control --reload
for c in /sys/class/drm/card*/device; do
  grep -q 0x1002 "$c/vendor" 2>/dev/null && echo high | sudo tee "$c/power_dpm_force_performance_level" >/dev/null || true
done

echo "== Ollama тримає моделі в пам'яті (64 ГБ є; інакше кожен запит після паузи платить ~2 хв за gemma4:26b)"
if [ -d /etc/systemd/system ] && systemctl list-unit-files ollama.service >/dev/null 2>&1; then
  sudo mkdir -p /etc/systemd/system/ollama.service.d
  printf "[Service]\nEnvironment=OLLAMA_KEEP_ALIVE=-1\n" | sudo tee /etc/systemd/system/ollama.service.d/keepalive.conf >/dev/null
  sudo systemctl daemon-reload && sudo systemctl restart ollama || true
fi

echo "== uv"
command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
export PATH="$HOME/.local/bin:$PATH"

echo "== whisper.cpp з Vulkan"
cd "$HOME"
[ -d whisper.cpp ] || git clone -q --depth 1 https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
[ -x build/bin/whisper-cli ] || {
  cmake -B build -DGGML_VULKAN=1 -DGGML_CCACHE=OFF -DCMAKE_BUILD_TYPE=Release >build.log 2>&1
  cmake --build build -j"$(nproc)" --config Release >>build.log 2>&1
}
[ -s models/ggml-large-v3.bin ] || sh models/download-ggml-model.sh large-v3
[ -s models/ggml-large-v3-q8_0.bin ] || ./build/bin/whisper-quantize models/ggml-large-v3.bin models/ggml-large-v3-q8_0.bin q8_0

echo "== pyannote на CPU (колесо torch для CPU; на Opteron без AVX2 працює)"
mkdir -p "$HOME/diar" && cd "$HOME/diar"
[ -x .venv/bin/python ] || uv venv -q --python 3.12 .venv
uv pip install -q --python .venv/bin/python \
  --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple \
  --index-strategy unsafe-best-match torch torchaudio pyannote-audio
uv cache clean -q

cat <<'EOF'

Готово. Далі вручну:
  1. покласти токен HF у ~/.hf_env (рядок HUGGINGFACE_TOKEN=..., chmod 600);
  2. скопіювати diarize.py у ~/diar/;
  3. перезайти по ssh, щоб підхопилась група render (перевірка: vulkaninfo --summary | grep deviceName).
Запуск транскрипції:  ~/whisper.cpp/build/bin/whisper-cli -m ~/whisper.cpp/models/ggml-large-v3-q8_0.bin -f file.wav -l uk -bs 5 -t 4 -mc 0 -otxt -osrt -oj -of out
EOF
