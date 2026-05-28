# 1D CNN Autoencoder 模型训练数学推理与训练逻辑

本文档说明当前 `bluebot-mini-wave-dataset` 中 1D CNN autoencoder / embedding analyzer 的数学依据、训练流程、输出含义和上线使用逻辑。目标是把超声波流量计的原始 ADC 波形转化为可解释的健康度与异常检测指标。

## 1. 建模目标

每一条超声 ADC 波形可以表示为长度为 \(T\) 的一维向量：

$$
\mathbf{x}_i =
[x_i(0), x_i(1), \ldots, x_i(T-1)]^\top \in \mathbb{R}^{T}
$$

当前数据中通常有 \(T=1024\)，对应 CSV 里的 `s_0 ... s_1023`。

目前数据主要是 `good` 样本，因此模型的第一阶段目标不是训练一个多分类器，而是学习“健康波形的正常分布”：

$$
\mathcal{D}_{train} =
\{\mathbf{x}_i \mid label_i = \text{good}\}
$$

训练完成后，对于任意新波形，模型输出：

$$
\mathbf{x}_i
\rightarrow
\mathbf{z}_i
\rightarrow
\mathbf{e}_i
\rightarrow
\hat{\mathbf{z}}_i
$$

其中：

- \(\mathbf{z}_i\)：预处理和归一化后的输入波形
- \(\mathbf{e}_i \in \mathbb{R}^d\)：CNN encoder 学到的低维 embedding
- \(\hat{\mathbf{z}}_i\)：decoder 重建出的波形
- \(r_i\)：重建误差，用来衡量“这条波形是否不像正常波形”
- \(d_i\)：embedding 最近邻距离，用来衡量“这条波形离历史正常波形有多远”

## 2. 预处理数学过程

### 2.1 Baseline 去除

流量计 ADC 波形可能存在直流偏置，所以先用前 \(B\) 个采样点估计每条波形的 baseline：

$$
b_i =
\operatorname{median}
\{x_i(t) \mid 0 \le t < B\}
$$

当前实现中 \(B=160\)。中心化后的波形为：

$$
y_i(t) = x_i(t) - b_i
$$

这样做的意义是让模型主要学习回波形状、噪声结构、振铃和耦合状态，而不是学习设备的 ADC 偏置。

### 2.2 Full 模式与 Gate 模式

当前训练脚本支持两种输入：

Full 模式使用全部采样点：

$$
\mathbf{y}_i^{full} =
[y_i(0), y_i(1), \ldots, y_i(T-1)]^\top
$$

Gate 模式只使用主回波附近窗口：

$$
\mathbf{y}_i^{gate} =
[y_i(g_s), y_i(g_s+1), \ldots, y_i(g_e-1)]^\top
$$

其中 \(g_s, g_e\) 来自训练集均值模板：先找到平均波形中的主峰，再截取峰值附近窗口。Full 模式适合保留全局异常信息，Gate 模式适合聚焦主回波区域。

### 2.3 鲁棒归一化

模型只使用训练集的 good 样本来估计幅度尺度：

$$
a =
\max \left(
Q_p(\{|y_i(t)|: i \in \mathcal{G}, t \in \mathcal{T}\}),
a_{min}
\right)
$$

其中：

- \(\mathcal{G}\)：训练集 good 样本
- \(Q_p\)：分位数，当前默认 \(p=99\)
- \(a_{min}\)：最小尺度，当前默认 \(0.05\)

归一化输入为：

$$
z_i(t) =
\operatorname{clip}
\left(
\frac{y_i(t)}{a},
-\gamma,
\gamma
\right)
$$

当前默认 \(\gamma=6\)。这个步骤可以避免极端异常值支配训练，同时保留主要波形形态。

## 3. 1D CNN Autoencoder 结构

模型由 encoder 和 decoder 两部分组成。

### 3.1 Encoder

Encoder 把一维波形压缩成低维 embedding：

$$
\mathbf{e}_i = f_\theta(\mathbf{z}_i) \in \mathbb{R}^{d}
$$

当前默认 \(d=64\)。1D 卷积层可写为：

$$
h^{(\ell)}_{i,k}(t)
=
\sigma
\left(
\sum_m
\sum_{\tau=-q}^{q}
w^{(\ell)}_{k,m,\tau}
h^{(\ell-1)}_{i,m}(t-\tau)
+ \beta^{(\ell)}_k
\right)
$$

其中：

- \(k\)：输出通道
- \(m\)：输入通道
- \(w\)：卷积核参数
- \(\beta\)：偏置项
- \(\sigma(u)=\max(0,u)\)：ReLU 激活函数

池化层把时间长度逐步下采样：

$$
\tilde{h}^{(\ell)}_{i,k}(u)
=
\max_{t \in \{2u, 2u+1\}}
h^{(\ell)}_{i,k}(t)
$$

当前模型使用三次 \(2\times\) pooling，所以长度从 \(T\) 变为：

$$
T' = \frac{T}{8}
$$

当 \(T=1024\) 时，\(T'=128\)。

### 3.2 Decoder

Decoder 从 embedding 重建归一化波形：

$$
\hat{\mathbf{z}}_i = g_\phi(\mathbf{e}_i)
$$

完整 autoencoder 为：

$$
\hat{\mathbf{z}}_i =
g_\phi(f_\theta(\mathbf{z}_i))
$$

如果一条新波形接近正常训练分布，模型应该能较好重建它；如果波形形态远离正常分布，重建误差通常会上升。

## 4. 训练目标函数

模型训练目标是最小化重建均方误差：

$$
\mathcal{L}(\theta,\phi)
=
\frac{1}{|\mathcal{G}|T}
\sum_{i \in \mathcal{G}}
\left\|
\mathbf{z}_i -
g_\phi(f_\theta(\mathbf{z}_i))
\right\|_2^2
+
\lambda \|\theta,\phi\|_2^2
$$

其中：

- 第一项是 reconstruction MSE
- 第二项是权重衰减，对应代码中的 `weight_decay`
- 优化器使用 AdamW
- 训练过程中记录 train loss 和 validation loss
- 最终保存 validation loss 最低的模型参数

直觉上，这个目标让模型学习一组低维坐标，使健康波形可以被压缩后再还原。

## 5. 为什么 Autoencoder 可以做异常检测

假设健康波形集中在某个低维正常流形 \(\mathcal{M}\) 附近。Autoencoder 学到的近似映射可以理解为：

$$
\mathbf{z}
\xrightarrow{f_\theta}
\mathbf{e}
\xrightarrow{g_\phi}
\hat{\mathbf{z}}
\approx
P_\mathcal{M}(\mathbf{z})
$$

其中 \(P_\mathcal{M}\) 是把输入投影到正常波形流形上的近似操作。

对健康样本：

$$
\mathbf{z}_i \approx P_\mathcal{M}(\mathbf{z}_i)
\Rightarrow
\|\mathbf{z}_i - \hat{\mathbf{z}}_i\|^2 \text{ 较小}
$$

对异常样本：

$$
\mathbf{z}_i \notin \mathcal{M}
\Rightarrow
\|\mathbf{z}_i - \hat{\mathbf{z}}_i\|^2 \text{ 可能升高}
$$

因此定义重建误差：

$$
r_i =
\frac{1}{T}
\sum_{t=0}^{T-1}
(z_i(t)-\hat{z}_i(t))^2
$$

这个指标回答的问题是：模型能不能把这条波形“画回去”。

## 6. Embedding 相似度推理

仅看重建误差还不够，因为某些异常波形也可能被 autoencoder 粗略重建。因此再引入 embedding 空间的相似度。

Encoder 输出：

$$
\mathbf{e}_i = f_\theta(\mathbf{z}_i)
$$

不同 embedding 维度的尺度可能不同，所以用训练集 good 样本做鲁棒标准化。对第 \(k\) 维：

$$
\mu_k =
\operatorname{median}
\{e_{i,k}: i \in \mathcal{G}\}
$$

$$
\sigma_k =
\max
\left(
1.4826 \cdot
\operatorname{median}
\{|e_{i,k}-\mu_k|: i \in \mathcal{G}\},
\epsilon
\right)
$$

标准化 embedding：

$$
\tilde{e}_{i,k}
=
\frac{e_{i,k}-\mu_k}{\sigma_k}
$$

然后在训练集 good 样本中寻找最近邻：

$$
d_i =
\min_{j \in \mathcal{G}, j \ne i}
\left\|
\tilde{\mathbf{e}}_i -
\tilde{\mathbf{e}}_j
\right\|_2
$$

\(d_i\) 越大，表示这条波形在 learned embedding 空间中越不像历史健康样本。

为了给客户展示更直观的相似度，系统还输出：

$$
c_i = \frac{1}{1+d_i}
$$

其中 \(c_i \in (0,1]\)，越接近 1 表示越像历史健康波形。

## 7. 阈值推导

训练完成后，系统在训练集 good 样本上统计分布，并使用高分位数作为阈值。

重建误差阈值：

$$
\tau^r_{suspect} =
Q_{99.5}(\{r_i: i \in \mathcal{G}\})
$$

$$
\tau^r_{anomaly} =
Q_{99.9}(\{r_i: i \in \mathcal{G}\})
$$

Embedding 距离阈值：

$$
\tau^d_{suspect} =
Q_{95}(\{d_i: i \in \mathcal{G}\})
$$

$$
\tau^d_{anomaly} =
Q_{99}(\{d_i: i \in \mathcal{G}\})
$$

这样做的好处是阈值来自当前设备和安装条件下的真实健康数据，不需要人为拍脑袋设固定阈值。

## 8. 判定逻辑

最终 CNN 状态判定为：

$$
state_i =
\begin{cases}
anomaly,
&
r_i \ge \tau^r_{anomaly}
\;\text{or}\;
d_i \ge \tau^d_{anomaly}
\\
suspect,
&
r_i \ge \tau^r_{suspect}
\;\text{or}\;
d_i \ge \tau^d_{suspect}
\\
normal,
&
\text{otherwise}
\end{cases}
$$

两个指标互补：

| 指标 | 数学含义 | 工程含义 |
| --- | --- | --- |
| \(r_i\) | reconstruction MSE | 波形是否难以被正常模型重建 |
| \(d_i\) | 标准化 embedding 最近邻距离 | 波形是否远离历史健康波形云 |
| \(c_i\) | \(1/(1+d_i)\) | 给客户看的相似度分数 |

## 9. 模型训练逻辑

当前训练脚本是 [train_cnn_embedding_analyzer.py](train_cnn_embedding_analyzer.py)，核心流程如下。

1. 读取 CSV。
   - 识别 `s_0 ... s_N` 作为波形采样列。
   - 读取非采样列作为 metadata，例如 label、SQ、时间戳等。

2. 构造训练集合。
   - 默认只使用 `label=good` 的样本训练。
   - 如果 CSV 没有 label 列，则退化为使用全部样本。
   - 默认 `train_fraction=0.70`。
   - 默认 `split_mode=interleaved`，用于打散时间顺序，检查同分布表现。
   - 可选 `split_mode=temporal`，用于检测随时间漂移时的泛化能力。

3. 学习输入表示。
   - 对每条波形做 baseline median 去除。
   - Full 模式使用完整 1024 点。
   - Gate 模式先从训练集模板中找到主回波窗口，再截取窗口。
   - 使用训练集 good 样本估计归一化尺度。

4. 训练 CNN autoencoder。
   - 输入 shape 为 `[batch, 1, T]`。
   - Encoder 输出 64 维 embedding。
   - Decoder 重建输入波形。
   - Loss 为 reconstruction MSE。
   - Optimizer 为 AdamW。
   - 每个 epoch 后计算 validation MSE。
   - 保存 validation MSE 最低的模型参数。

5. 训练后全量打分。
   - 对所有分析样本计算 embedding。
   - 对所有分析样本计算 reconstruction MSE。
   - 用训练集 embedding 拟合 median/MAD 标准化参数。
   - 构建训练集 reference embedding bank。
   - 对每条样本寻找最近邻 healthy waveform。

6. 生成阈值与报告。
   - 从训练集分布得到 suspect/anomaly 阈值。
   - 输出 top reconstruction outliers。
   - 输出 top similarity outliers。
   - 输出每行样本的 CNN 状态、embedding、最近邻和 metadata。

7. 保存模型与产物。
   - `cnn_autoencoder_model.pt`
   - `cnn_embedding_report.json`
   - `cnn_embedding_analysis.jsonl`

## 10. Hugging Face 云端训练逻辑

云端训练由 [cloud/hf_submit_cnn_job.py](cloud/hf_submit_cnn_job.py) 提交。流程如下：

```text
本地 CSV
  -> 上传到 HF Dataset repo
  -> 提交 HF Job
  -> Job 下载 CSV
  -> Job 安装 numpy / torch / huggingface_hub
  -> Job 运行 train_cnn_embedding_analyzer.py
  -> Job 上传训练产物到 HF Model repo
```

典型命令：

```bash
.venv/bin/python cloud/hf_submit_cnn_job.py \
  --dataset-repo kx2090/bluebot-waveforms \
  --csv-path BB8100017587.csv \
  --output-repo kx2090/bluebot-meter-cnn \
  --flavor cpu-upgrade \
  --timeout 2h \
  --epochs 12 \
  --batch-size 128
```

云端训练的价值是：

- 本地机器不用长时间占用 CPU/GPU。
- 每次训练都有独立 `runs/<UTC timestamp>` 目录。
- 模型、报告和逐行分析可以版本化保存。
- 训练和实时推理之间有明确 review gate，避免模型未验证就上线。

## 11. 输出文件解释

### 11.1 `cnn_autoencoder_model.pt`

这是运行时使用的模型 checkpoint，包含：

- CNN 模型参数 `state_dict`
- 输入长度、embedding 维度、input mode
- baseline / normalization 参数
- reconstruction thresholds
- embedding distance thresholds
- embedding metric median/MAD
- reference embedding bank

实时 MQTT 分析使用这个文件进行在线评分。

### 11.2 `cnn_embedding_report.json`

这是训练级别报告，适合做模型验证和客户摘要。主要内容包括：

- 训练行数、验证行数、总行数
- 训练历史 `train_loss / validation_loss`
- reconstruction MSE 分位数
- embedding similarity / distance 分位数
- suspect/anomaly 阈值
- top outlier 样本

### 11.3 `cnn_embedding_analysis.jsonl`

这是逐行分析文件，每行对应一个 waveform。常见字段包括：

- `row`
- `metadata`
- `cnn_state`
- `reconstruction_mse`
- `nearest_embedding_distance`
- `nearest_similarity`
- `nearest_neighbors`
- `embedding`

它适合后续做图表、抽样复盘、客户报告和误报分析。

## 12. 实时推理逻辑

实时推理由 [cnn_embedding_runtime.py](cnn_embedding_runtime.py) 和 [mqtt_stream_analyzer.py](mqtt_stream_analyzer.py) 完成。

对每条 MQTT 波形：

1. 使用 checkpoint 里的同一套 baseline / gate / normalization 参数预处理。
2. 输入 CNN autoencoder。
3. 得到 reconstruction MSE 和 embedding。
4. 在 reference embedding bank 中寻找最近邻。
5. 输出 `cnn_analysis`。
6. 将 CNN 分数与 one-class 特征模型一起融合到 `flow_meter_health`。

运行示例：

```bash
.venv/bin/python mqtt_stream_analyzer.py \
  --broker mqtt-prod.bluebot.com \
  --port 1883 \
  --client-id 'lens_cnn_{uuid}' \
  --sig-topic meter/sig/BB8100017587 \
  --pub-topic meter/pub/BB8100017587 \
  --processed-topic "" \
  --model oneclass_meter_model_mode.json \
  --cnn-model cnn_autoencoder_model.pt \
  --cnn-device auto \
  --cnn-health-weight 0
```

## 13. 与流量计健康度的关系

CNN 模型本身输出的是“波形形态可信度”，不是流量值。它回答的问题是：

```text
这条声学测量是否像历史健康测量？
```

`flow_meter_health.py` 会把多个维度合成客户可读的健康度：

$$
H =
\sum_k w_k s_k
$$

当前子分数包括：

- signal integrity
- acoustic pattern match
- coupling condition
- temporal stability
- telemetry reliability

其中 `acoustic_pattern_match` 会融合：

- one-class 手写特征异常分数
- CNN reconstruction MSE
- CNN nearest embedding distance

因此 CNN 是健康度中的“声学波形模式匹配”证据来源之一。

现场测试发现同一只表可能存在多个稳定 good 波形模式，例如峰值锁定在不同 echo lobe。新版 one-class 模型会按 `peak_mode` 分别保存 robust feature stats 和 score thresholds：

$$
m_i = g(\Delta p_i),
\quad
\Delta p_i = p_i - p_0
$$

在线打分时先确定模式：

$$
s_i =
\sqrt{
\frac{1}{d}
\sum_{j=1}^{d}
\min\left(
\left|
\frac{x_{ij}-\mu_{m_i,j}}
{\sigma_{m_i,j}}
\right|,
20
\right)^2
}
$$

如果当前模式没有足够训练样本，则回退到全局 baseline。这样可以把“多种正常波形形态”与真正异常分开。

## 14. 给客户的解释口径

可以这样描述：

> 我们先用历史健康波形训练一个 1D CNN autoencoder，让模型学习设备在正常安装和正常介质状态下的声学波形基线。在线运行时，每条新波形都会被压缩成一个 embedding，并与历史健康波形库做相似度比较。同时模型会尝试重建这条波形，如果重建困难或 embedding 距离健康库过远，就说明该测量的声学可信度下降。最终系统把该证据与 SNR、模板相关性、削顶、遥测新鲜度等指标融合为流量计健康度。

更短的客户版本：

> 模型不是直接猜流量，而是在判断这一次声学测量是否可信。它学习健康波形的形状基线，并在实时数据中寻找偏离健康基线的波形。

## 15. 局限性与下一步

当前模型是 one-class / unsupervised 第一阶段，因此它擅长判断“不像 healthy”，但不能天然区分所有异常类型。

主要局限：

- 如果训练集中混入异常样本，模型可能把异常也学成正常。
- 如果设备安装条件变化很大，阈值需要重新训练或校准。
- 如果未来存在多种正常工况，需要收集覆盖更广的 good 样本。
- 当前分类标签是 `normal / suspect / anomaly`，不是完整故障类型分类。

下一步建议：

1. 对每个设备维护独立 healthy baseline。
2. 收集人工确认的 air、empty pipe、weak signal、clipping 等负样本。
3. 在 CNN embedding 上训练轻量监督分类器：

$$
p_\psi(y_i \mid \mathbf{e}_i),
\quad
y_i \in
\{\text{normal}, \text{weak}, \text{air}, \text{empty}, \text{noise}, \text{clipping}\}
$$

4. 比较 full input 与 gate input 的误报率和召回率。
5. 将训练报告中的 top outliers 做人工复核，形成可解释样本库。

## 16. 现场 Good 数据闭环

现场确认某段采集数据为 good 后，不建议直接覆盖旧模型。推荐流程是先合并、再训练、再离线审计：

```bash
.venv/bin/python merge_waveform_csvs.py \
  --output training_BB8100017587_combined.csv \
  --input BB8100017587.csv:lab_original \
  --input live_mode_test.csv:field_live_20260528 \
  --confirmed-good \
  --force-label good
```

训练新的 mode-aware one-class 模型：

```bash
.venv/bin/python train_oneclass_meter_model.py training_BB8100017587_combined.csv \
  --labels good \
  --model-out oneclass_meter_model_combined.json \
  --report-out oneclass_meter_report_combined.json \
  --synthetic-rows 500
```

对现场 CSV 做离线审计：

```bash
.venv/bin/python audit_live_csv.py live_mode_test.csv \
  --model oneclass_meter_model_combined.json \
  --cnn-model cnn_autoencoder_model.pt \
  --cnn-device cpu \
  --cnn-health-weight 0 \
  --json-out audit_live_mode_test_combined.json \
  --markdown-out audit_live_mode_test_combined.md
```

如果审计结果满足以下条件，才建议切换 MQTT 实时模型：

- `stable_states` 大部分或全部为 `normal_acoustic_state`
- `stable_transitions` 接近 0
- `synthetic_probes` 仍保持高检出率
- `health_labels` 主要集中在 `Healthy` / `Excellent`
- `Watch` / `Degraded` 行能从 `top_reasons` 中解释，例如短时噪声、削顶、gate energy 变化

实时命令建议使用 combined 模型，并继续让旧 CNN 旁路输出：

```bash
.venv/bin/python mqtt_stream_analyzer.py \
  --broker mqtt-prod.bluebot.com \
  --port 1883 \
  --client-id 'lens_cnn_{uuid}' \
  --sig-topic meter/sig/BB8100017587 \
  --pub-topic meter/pub/BB8100017587 \
  --processed-topic "" \
  --model oneclass_meter_model_combined.json \
  --cnn-model cnn_autoencoder_model.pt \
  --cnn-device auto \
  --cnn-health-weight 0 \
  --heartbeat-s 10 \
  --save-csv live_next.csv
```
