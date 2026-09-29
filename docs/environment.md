# 环境与复现

当前安装：`~/miniforge3`，环境 `tts-data`，Python 3.11。
已通过 `conda init bash` 在 `~/.bashrc` 写入 Conda 管理区块，base 不自动激活。
新终端可直接使用 conda；当前终端执行：

```bash
source ~/.bashrc
conda activate tts-data
```

Miniforge 版本 `26.7.2-0`，Linux x86_64 安装器 SHA256：
`281b0ac7d550802efc81af633225a5e6116d29ae72f3ab4eae7168c3931a4c05`。
下载安装器后与镜像上的对应版本 SHA256 文件核对，再安装。

Conda 使用 USTC conda-forge，strict channel priority；pip 的 USTC index
写在 **tts-data 环境内的 pip.conf**，没有改全局 pip 配置。
参照 [USTC Conda 说明](https://mirrors.ustc.edu.cn/help/anaconda.html) 与
[USTC PyPI 说明](https://mirrors.ustc.edu.cn/help/pypi.html)。
镜像缓存未命中时可能重定向到其他镜像。

## 创建类似环境

```bash
conda env create -f environment.yml
conda activate tts-data
python -m pip config --site set global.index-url https://mirrors.ustc.edu.cn/pypi/simple
python -m pip install -r requirements.txt
```

`environment.yml` 与 requirements.txt 声明依赖范围，不是完整版本锁定。
本机实际解析结果另存 `configs/conda-linux64.explicit.txt` 和 `configs/requirements.lock.txt`。
相同 Linux 平台精确复现：

```bash
conda create -n tts-data-repro --file configs/conda-linux64.explicit.txt
conda activate tts-data-repro
python -m pip install -i https://mirrors.ustc.edu.cn/pypi/simple \
  -r configs/requirements.lock.txt
python scripts/tts_data.py --help
```

基础转换使用 pylance==12.0.0、PyArrow、SoundFile；不需要 CUDA、Torch 或 TorchCodec。
原音频 bytes 直接写入 Lance，schema 使用原生 Arrow，不依赖 HF datasets 的 Audio 编码路径。
Lance 文件格式固定 2.2；这与数据发布 v0.1 是两个不同版本概念。
codec/GPU 批处理使用单独环境锁定模型、Torch、decoder 和重采样实现。

项目不作为 distribution 安装，无需 pip install -e .。
入口脚本加载仓库内 src 模块，pyproject.toml 保存 pytest 与格式检查配置。
两个环境均保留 pytest 依赖；当前回归命令见仓库 README，历史 lock 保留实际安装快照。

## Codec / speaker 独立环境

新环境为 `~/miniforge3/envs/tts-features`，与基础转换的 `tts-data` 分开。
使用 Python 3.11、Torch/torchaudio 2.8.0+cu126、qwen-tts 0.1.1、transformers 4.57.3、
numpy 1.26.4、scipy 1.15.3、SoundFile 0.13.1、Lance 12.0.0、Arrow 25.0.0。
不要在这里安装根目录的 requirements.txt（它为基础转换声明 numpy>=2）；使用 features 专属文件。

```bash
conda env create -f configs/features-environment.yml
conda activate tts-features
python -m pip config --site set global.index-url https://mirrors.ustc.edu.cn/pypi/simple
python -m pip install torch==2.8.0+cu126 torchaudio==2.8.0+cu126 \
  --index-url https://download.pytorch.org/whl/cu126
python -m pip install -r configs/features-requirements.txt
python -m pip check
```

CUDA wheels 从 PyTorch 官方索引安装，其余 pip 包使用环境内 USTC 配置，不改全局 pip 或旧环境。
已安装 flash-attn 2.8.3.post1（官方 Torch2.8/cu12/C++11 ABI wheel，requirements 固定 URL 与 SHA256）。
codec 正式 C 使用 FP16/FA2 + FP32 码本缓存，配置与验收见 [codec 执行说明](codec.md)；ECAPA speaker 不适用 FA2。
当前主机为 8 × A800 80GB；沙箱内可能无法看到 /dev/nvidia*，GPU 检查与执行需使用设备可见的上下文。
环境就绪不代表已完成真实权重/profile 验收，步骤见 [提取计划](design-review/feature-extraction-plan.md)。

2026-09-29 北京时间验证：pip check 通过，tokenizer/speaker/frontend 导入成功；8 卡均通过小矩阵运算和
合成 mel 计算，SoundFile FLAC、scipy/torchaudio 重采样及 Lance 读写通过。新环境的 31 项 contract/feature
存储与 manifest 回归测试通过。该次检查未加载真实模型权重、未提取数据特征。随后模型已下载并记录 sources.json；该段仅记录最初环境检查；当前真实 codec 验收与运行状态见 codec 执行说明。

实际解析结果已锁定于 configs/features-conda-linux64.explicit.txt 与 configs/features-requirements.lock.txt。
同平台复现时先用 Conda explicit 文件创建环境，再安装上面的 cu126 wheels，最后安装 pip lock：

```bash
conda create -n tts-features-repro --file configs/features-conda-linux64.explicit.txt
conda activate tts-features-repro
python -m pip install torch==2.8.0+cu126 torchaudio==2.8.0+cu126 \
  --index-url https://download.pytorch.org/whl/cu126
python -m pip install -i https://mirrors.ustc.edu.cn/pypi/simple \
  -r configs/features-requirements.lock.txt
python -m pip check
```
