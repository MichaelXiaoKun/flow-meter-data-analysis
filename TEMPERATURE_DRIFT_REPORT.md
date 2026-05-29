# 超声波流量计温漂与零点补偿报告

日期：2026-05-29

## 1. 摘要

温漂是指流量计在温度变化过程中，即使真实流量接近零，测得的流速或流量仍然出现缓慢偏移的现象。在超声波流量计中，温漂通常不是单一的显示问题，而是声速、换能器延迟、电子链路延迟、管道耦合状态和算法零点共同作用的结果。

本项目建议将温漂的主补偿量建模在 `fs`（flow speed，单位 `m/s`）上，再通过管道截面积换算成 `fr`（flow rate，单位 `m3/h`）。原因是温度引起的零点偏移更接近速度或传播时间差误差，而不是独立的体积计量误差。若同时独立学习 `fs_zero` 和 `fr_zero`，两套补偿可能相互冲突，尤其在管径配置或面积换算存在误差时会放大问题。

推荐的核心公式为：

```text
fs_corrected = fs_raw - fs_zero_drift
fr_corrected = fs_corrected * area_m2 * 3600
```

其中：

```text
area_m2 = pi * pipe_inner_diameter_m^2 / 4
pipe_inner_diameter_mm = pipe_outer_diameter_mm - 2 * pipe_wall_thickness_mm
```

## 2. 温漂来源

超声波流量计的温漂主要来自以下几类因素：

1. 声速变化
   温度改变会影响介质声速，进而影响上下游传播时间与总飞行时间。对于依赖时间差的测量方式，微小的时间偏移就可能转换成可见的低流量读数。

2. 换能器与电子延迟变化
   换能器、模拟前端、比较器、ADC 采样链路可能随温度产生延迟漂移。该漂移往往与板载温度更相关，而不一定与环境温度或流体温度完全一致。

3. 热滞后与升降温不对称
   设备升温和降温时，外壳、管壁、传感器、流体之间存在热惯性。即使温度读数相同，正在升温和正在降温的零点偏移也可能不同。

4. 耦合状态变化
   管壁、耦合材料、安装夹具在温度变化下可能改变声学耦合，使波形能量、噪声、模板相关性发生变化。这类变化不应被简单当成流量。

5. 算法零点随时间漂移
   长时间运行后，安装状态、电子基线、环境条件可能缓慢变化，需要低速自适应零点，但必须有严格的无流证据作为保护。

## 3. 本项目现有基础

当前代码已经具备温漂补偿所需的几个关键能力。

`meter/pub` 侧可提供板载温度 `flow.ots`，运行时代码会映射为：

```text
onboard_temperature_c
ots
```

同时，发布侧还可能提供：

```text
pub_fs_mps
pub_fr_m3h
diagnose_dt_ns
diagnose_tt_ns
pipe_outer_diameter_mm
pipe_wall_thickness_mm
pipe_area_from_geometry_m2
```

这些字段可以用于分离三件事：

```text
diagnose_tt_ns -> 更接近声速、路径和热状态
diagnose_dt_ns -> 更接近上下游时间差和零点偏移
fs/fr         -> 客户可见的流速和流量结果
```

现有实时分析器还会从波形中提取：

```text
template_corr
noise_rms_v
gate_rms_v
snr_db
peak_abs_gate_v
peak_offset_samples
first_arrival_offset_samples
low_clip_ratio
high_clip_ratio
```

这些声学特征可以用于判断温度变化期间读数是否仍然可信。例如，温度变化同时伴随 `template_corr` 下降、噪声上升或峰值位置漂移，则更像声学状态变化或安装耦合变化，而不应直接做零点学习。

## 4. 建模方案

建议将零点漂移拆成两部分：

```text
fs_zero_drift = fs_temp_model(T, dT/dt, direction) + fs_adaptive_zero
```

其中 `fs_temp_model` 表示可解释的温度项，`fs_adaptive_zero` 表示慢速残差学习。

### 4.1 温度模型

板载温度 `OTS` 应作为主要热状态变量：

```text
onboard_temp_delta_c = onboard_temperature_c - onboard_temp_reference_c
onboard_temp_rate_c_per_min = d(onboard_temperature_c) / dt
```

考虑热滞后，升温和降温建议使用不同系数：

```text
warming:
fs_temp_model = a_heat * onboard_temp_delta_c
              + b_heat * onboard_temp_rate_c_per_min

cooling:
fs_temp_model = a_cool * onboard_temp_delta_c
              + b_cool * onboard_temp_rate_c_per_min
```

如果未来同时有管壁温度或流体温度，还应保留热梯度：

```text
thermal_gradient_c = onboard_temperature_c - pipe_or_fluid_temperature_c
```

热梯度可以解释同一 OTS 温度下，声学路径实际热状态不同的问题。

### 4.2 慢速自适应零点

自适应项只应在强无流证据下更新：

```text
residual_fs = fs_raw - fs_temp_model
fs_adaptive_zero = EWMA(residual_fs)
```

建议使用两档学习率：

```text
用户确认 zero_flow: 较高学习率
机器判定高置信 zero_flow: 较低学习率
```

并设置保护：

```text
max_abs_zero_fs_mps
max_update_fs_mps_per_min
min_event_probability_to_freeze_learning
```

只要检测到真实用水事件、声学异常、数据断流、SQ 过低或发布侧时间戳过旧，就应冻结零点学习。

## 5. 判定逻辑

温度变化本身不能等同于零流，也不能自动把小流量归零。推荐逻辑是同时估计三个概率：

```text
phantom_flow_probability
event_flow_probability
zero_flow_probability
```

可抑制 phantom flow 的条件应同时满足：

```text
phantom_flow_probability 高
event_flow_probability 低
abs(fs_corrected) 在低流量死区内
波形漂移与温度模型一致
数据连续且遥测新鲜
```

真实用水事件必须阻止零点学习：

```text
event_flow_probability 高 -> freeze zero learning
```

数据中断也必须阻止零点学习，不应把断线期间显示为 0 流量：

```text
data_gap -> freeze phantom/event decisions, zero tracking, self-training
```

## 6. 输出字段建议

每一帧建议输出以下字段，便于调试、客户解释和后续审计：

```text
fs_raw
fr_raw
onboard_temperature_c
onboard_temp_reference_c
onboard_temp_delta_c
onboard_temp_rate_c_per_min
pipe_or_fluid_temperature_c
thermal_gradient_c
fs_temp_model
fs_adaptive_zero
fs_zero_drift
fr_zero_drift
fs_corrected
fr_corrected
fs_published
fr_published
phantom_flow_probability
event_flow_probability
zero_flow_probability
measurement_confidence
zero_learning_frozen
zero_learning_freeze_reason
```

其中 `fs_mps` 应作为权威补偿量，`fr_m3h` 可以作为派生值保存，方便 UI 和报告显示。

## 7. 数学建模表达

设第 \(t\) 帧的板载温度、原始流速和原始体积流量分别为：

```text
T_t = onboard_temperature_c
v_t = fs_raw
q_t = fr_raw
```

设管道截面积为 \(A\)，真实流速为 \(u_t\)，温漂造成的零点偏置为 \(z_t\)，观测噪声为 \(\epsilon_t\)。原始流速观测模型可写成：

\[
v_t = u_t + z_t + \epsilon_t
\]

体积流量由流速换算：

\[
q_t = 3600 A v_t
\]

其中：

\[
A = \frac{\pi D_i^2}{4}
\]

\[
D_i = \frac{D_{outer} - 2w}{1000}
\]

这里 \(D_{outer}\) 是管道外径，\(w\) 是管壁厚度，单位为 `mm`；\(D_i\) 转换为 `m` 后用于面积计算。

温漂零点偏置拆成温度模型和慢速自适应残差：

\[
z_t = f_T(T_t, \dot T_t, s_t) + a_t
\]

其中温度变化率为：

\[
\dot T_t = \frac{T_t - T_{t-1}}{\Delta t}
\]

升温/降温状态为：

\[
s_t =
\begin{cases}
heat, & \dot T_t \ge 0 \\
cool, & \dot T_t < 0
\end{cases}
\]

考虑热滞后，温度项使用分段线性模型：

\[
f_T(T_t, \dot T_t, s_t) =
\begin{cases}
\alpha_h (T_t - T_0) + \beta_h \dot T_t, & \dot T_t \ge 0 \\
\alpha_c (T_t - T_0) + \beta_c \dot T_t, & \dot T_t < 0
\end{cases}
\]

其中 \(T_0\) 是参考温度，\(\alpha_h,\beta_h\) 是升温系数，\(\alpha_c,\beta_c\) 是降温系数。

修正后的真实流速估计为：

\[
\hat u_t = v_t - z_t
\]

修正后的体积流量为：

\[
\hat q_t = 3600 A \hat u_t
\]

在确认零流或高置信零流时，有：

\[
u_t \approx 0
\]

因此：

\[
v_t \approx z_t + \epsilon_t
\]

温度模型解释后的残差为：

\[
r_t = v_t - f_T(T_t, \dot T_t, s_t)
\]

慢速自适应零点使用 EWMA 更新：

\[
a_t = (1-\lambda)a_{t-1} + \lambda r_t
\]

但该更新只允许在安全无流条件成立时发生。定义安全更新门控函数：

\[
G_t =
\mathbf{1}
\left[
P_{zero}(t) > \tau_z
\land
P_{event}(t) < \tau_e
\land
|\hat u_t| < \delta_v
\land
C_t = 1
\right]
\]

其中 \(C_t=1\) 表示数据连续、遥测新鲜、波形健康、无数据断流。于是自适应项完整更新式为：

\[
a_t =
\begin{cases}
(1-\lambda)a_{t-1} + \lambda r_t, & G_t = 1 \\
a_{t-1}, & G_t = 0
\end{cases}
\]

最终发布流速可写成：

\[
v_{pub,t} =
\begin{cases}
0, &
P_{phantom}(t) > \tau_p
\land P_{event}(t) < \tau_e
\land |\hat u_t| < \delta_v
\\
\hat u_t, & otherwise
\end{cases}
\]

发布体积流量为：

\[
q_{pub,t} = 3600 A v_{pub,t}
\]

因此，本项目温漂补偿的核心数学表达是：

\[
\boxed{
v_t = u_t + f_T(T_t,\dot T_t,s_t) + a_t + \epsilon_t
}
\]

需要估计并减去的零点漂移为：

\[
\boxed{
z_t = f_T(T_t,\dot T_t,s_t) + a_t
}
\]

最终得到真实流速估计：

\[
\boxed{
\hat u_t = v_t - z_t
}
\]

该表达说明：温漂应作为加在 `fs` 上的零点偏置项处理，再由管道面积推导 `fr`，不建议在 `fs` 和 `fr` 上分别学习互不约束的零点。

## 8. 验证方法

建议分三阶段验证。

### 8.1 静态零流温度实验

在确认无流状态下记录至少一次完整升温和降温过程，采集：

```text
OTS
fs_raw
fr_raw
diagnose_dt_ns
diagnose_tt_ns
template_corr
noise_rms_v
gate_rms_v
snr_db
```

目标是拟合：

```text
fs_raw ≈ fs_temp_model(T, dT/dt, direction) + residual
```

并比较补偿前后的零点漂移幅度：

```text
raw_zero_error = median(abs(fs_raw))
corrected_zero_error = median(abs(fs_corrected))
improvement = 1 - corrected_zero_error / raw_zero_error
```

### 8.2 真实用水事件保护测试

在温度变化过程中引入真实流量事件，确认模型不会把真实小流量误压成零。重点观察：

```text
event_flow_probability
phantom_flow_probability
fs_corrected
published_fs_mps
zero_learning_freeze_reason
```

合格标准是：事件期间零点学习冻结，发布流量不被 phantom suppression 错误抑制。

### 8.3 长时间在线 A/B 测试

在 Railway 实时原型中保留两路结果：

```text
未补偿 raw fs/fr
温漂补偿 corrected fs/fr
最终发布 published fs/fr
```

同时保存 append-only 事件记录，确保任何一次自动抑制都能追溯到当时的温度、波形、概率、阈值和用户标签。

## 9. 风险与保护

1. 误把真实低流量当作温漂
   这是最大风险。必须使用事件概率、波形可信度、SQ、连续帧和用户标签共同保护。

2. 温度传感器不代表真实声学路径温度
   OTS 更接近板载电子和换能器热状态，但不一定等于流体温度。因此模型需要保留热梯度字段，并允许后续加入管壁或流体温度。

3. 升温和降温模型混用
   热滞后会导致同温不同偏移。建议从一开始就区分 warming/cooling。

4. 数据间断造成虚假零流
   数据断流时不能把流量显示为 0，也不能更新零点。需要明确标记 `data_gap` 并冻结学习。

5. 管径配置错误影响 `fr`
   因为 `fr` 由 `fs * area_m2 * 3600` 得到，管径或壁厚错误会导致体积流量偏差。零点模型应优先在 `fs` 上学习，避免把面积配置错误混入温漂补偿。

## 10. 推荐实施路径

第一步，统一字段。
确保实时记录中稳定保存 `pub_fs_mps`、`pub_fr_m3h`、`onboard_temperature_c/ots`、`diagnose_dt_ns`、`diagnose_tt_ns`、管道几何和声学特征。

第二步，离线拟合初始温度模型。
使用用户确认的零流片段拟合 `a_heat`、`b_heat`、`a_cool`、`b_cool`，输出每台表独立的初始模型。

第三步，上线慢速残差学习。
只在高置信零流、遥测新鲜、波形健康、无数据间断时更新 `fs_adaptive_zero`。

第四步，保留人工反馈闭环。
UI 中继续支持 `zero_flow`、`event_flow`、`unsure` 标签。用户确认 zero flow 后可提高学习率；用户确认 event flow 后应冻结零点并降低相似场景的 phantom suppression。

第五步，生成客户可读报告。
每天输出温度范围、零点漂移估计、自动抑制次数、人工确认次数、补偿前后低流量误差等指标。

## 11. 结论

温漂补偿不应被实现成简单的“温度变化时把小流量归零”。更稳妥的方案是：

```text
先在 fs 上建立温度零点模型
再用强无流证据慢速学习残差
最后把修正后的 fs 换算成 fr
```

这种方案有三个优点：

1. 物理含义清楚：温漂主要对应速度/时间差偏移。
2. 工程风险较低：真实事件、数据断流和声学异常都能阻止错误学习。
3. 可审计：每次补偿都能追溯到温度、波形、概率、阈值和用户标签。

建议将本报告作为温漂原型进入实时测试的设计基线，并用零流升降温实验验证补偿收益。
