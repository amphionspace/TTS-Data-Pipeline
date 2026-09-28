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
入口脚本加载仓库内 src 模块，pyproject.toml 只保存测试和格式检查配置。
