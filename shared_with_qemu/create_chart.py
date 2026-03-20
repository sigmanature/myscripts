# import pandas as pd
# import io
# import pyvista as pv
# import numpy as np
# from matplotlib.cm import get_cmap

# # 1. 数据准备 (无变化)
# csv_data = """产品,季度,销售额
# Alpha,Q1,120
# Alpha,Q2,150
# Alpha,Q3,180
# Alpha,Q4,220
# Beta,Q1,90
# Beta,Q2,110
# Beta,Q3,130
# Beta,Q4,160
# Gamma,Q1,200
# Gamma,Q2,190
# Gamma,Q3,210
# Gamma,Q4,250
# """
# df = pd.read_csv(io.StringIO(csv_data))
# product_labels = df['产品'].unique()
# quarter_labels = ['Q1', 'Q2', 'Q3', 'Q4']
# product_map = {label: i for i, label in enumerate(product_labels)}
# quarter_map = {label: i for i, label in enumerate(quarter_labels)}
# df['product_pos'] = df['产品'].map(product_map)
# df['quarter_pos'] = df['季度'].map(quarter_map)

# # 2. 定义世界空间和缩放比例 (无变化)
# TARGET_AXIS_LENGTH = 8.0
# max_x_data = len(product_labels) - 1
# max_y_data = len(quarter_labels) - 1
# max_z_data = df['销售额'].max()
# x_scale = TARGET_AXIS_LENGTH / max_x_data if max_x_data > 0 else 1.0
# y_scale = TARGET_AXIS_LENGTH / max_y_data if max_y_data > 0 else 1.0
# z_scale = TARGET_AXIS_LENGTH / max_z_data if max_z_data > 0 else 1.0

# # 3. 初始化 PyVista 场景
# # 修复2: 不再使用预设光照，我们将手动添加
# plotter = pv.Plotter(window_size=[1200, 900])
# plotter.background_color = '#1E1E1E'

# # 4. 循环数据，创建并添加样式优化后的柱状图
# cmap = get_cmap('plasma') 
# bar_width_world = 1.8 
# x_offset = bar_width_world / 2
# y_offset = bar_width_world / 2

# for _, row in df.iterrows():
#     x_data, y_data, z_data = row['product_pos'], row['quarter_pos'], row['销售额']
    
#     x_world = x_data * x_scale + x_offset
#     y_world = y_data * y_scale + y_offset
#     z_world = z_data * z_scale
    
#     bounds = (
#         x_world - bar_width_world / 2, x_world + bar_width_world / 2,
#         y_world - bar_width_world / 2, y_world + bar_width_world / 2,
#         0, z_world
#     )
#     bar = pv.Box(bounds=bounds)

#     normalized_color_value = (z_data - df['销售额'].min()) / (max_z_data - df['销售额'].min())
#     color = cmap(normalized_color_value)
    
#     plotter.add_mesh(bar, color=color, 
#                      smooth_shading=False,  # <--- 正确的参数！
#                      specular=0.7,
#                      specular_power=30)

# # 5. 绘制世界坐标系中的轴和标签 (无变化)
# # ... (这部分代码和之前一样，为了简洁省略，你可以直接用上一版的)
# axis_color = '#CCCCCC'
# label_color = '#DDDDDD'
# axis_len_x = TARGET_AXIS_LENGTH + x_offset * 2
# axis_len_y = TARGET_AXIS_LENGTH + y_offset * 2
# axis_len_z = TARGET_AXIS_LENGTH
# plotter.add_mesh(pv.Line((0, 0, 0), (axis_len_x, 0, 0)), color=axis_color, line_width=3)
# plotter.add_mesh(pv.Line((0, 0, 0), (0, axis_len_y, 0)), color=axis_color, line_width=3)
# plotter.add_mesh(pv.Line((0, 0, 0), (0, 0, axis_len_z)), color=axis_color, line_width=3)
# arrow_size = 0.3
# arrow_height = 0.5
# plotter.add_mesh(pv.Cone(center=(axis_len_x + arrow_height / 2, 0, 0), direction=(1, 0, 0), height=arrow_height, radius=arrow_size), color='yellow')
# plotter.add_mesh(pv.Cone(center=(0, axis_len_y + arrow_height / 2, 0), direction=(0, 1, 0), height=arrow_height, radius=arrow_size), color='yellow')
# plotter.add_mesh(pv.Cone(center=(0, 0, axis_len_z + arrow_height), direction=(0, 0, 1), height=arrow_height * 1.5, radius=arrow_size * 1.2), color='yellow')
# for i, label in enumerate(product_labels):
#     plotter.add_point_labels([i * x_scale + x_offset, -0.5, 0], [label], text_color=label_color, font_size=10, show_points=False, shape=None)
# plotter.add_point_labels([(TARGET_AXIS_LENGTH / 2) + x_offset, -1.5, 0], ["产品类型"], text_color=label_color, font_size=14, bold=True, show_points=False, shape=None)
# for i, label in enumerate(quarter_labels):
#     plotter.add_point_labels([-0.5, i * y_scale + y_offset, 0], [label], text_color=label_color, font_size=10, show_points=False, shape=None)
# plotter.add_point_labels([-1.5, (TARGET_AXIS_LENGTH / 2) + y_offset, 0], ["季度"], text_color=label_color, font_size=14, bold=True, show_points=False, shape=None)
# z_ticks_data = np.linspace(0, max_z_data, 6)
# for tick_data in z_ticks_data:
#     if tick_data > 0:
#         z_pos_world = tick_data * z_scale
#         plotter.add_point_labels([-0.5, -0.5, z_pos_world], [f"{int(tick_data)}"], text_color=label_color, font_size=10, show_points=False, shape=None)
# plotter.add_point_labels([-1.0, -1.0, axis_len_z + 1.0], ["销售额 (万元)"], text_color=label_color, font_size=14, bold=True, show_points=False, shape=None)


# # 6. 设置相机、光照和坐标轴小部件
# # 修复1: 添加一个永远可见的坐标轴小部件
# plotter.add_axes()

# # 修复2: 添加一个跟随相机的“头灯”
# plotter.add_light(pv.Light(light_type='headlight'))

# # # 修复4: (可选) 尝试重新启用阴影
# # # 如果下面的代码导致程序崩溃或出现渲染错误，请再次注释掉它
# # try:
# #     plotter.enable_shadows()
# # except Exception as e:
# #     print(f"启用阴影失败 (可能是显卡驱动不兼容): {e}")


# plotter.camera_position = 'xz' # 恢复到斜向视角，这样更能看出3D效果
# plotter.enable_parallel_projection()
# plotter.camera.zoom(1.1)

# # 7. 显示场景
# plotter.show()
import plotly.graph_objects as go
import pandas as pd
import io

# ─────────────────── 1. 准备数据 ────────────────────
csv_data = """产品,季度,销售额
Alpha,Q1,120
Alpha,Q2,150
Alpha,Q3,180
Alpha,Q4,220
Beta,Q1,90
Beta,Q2,110
Beta,Q3,130
Beta,Q4,160
Gamma,Q1,200
Gamma,Q2,190
Gamma,Q3,210
Gamma,Q4,250
"""
df = pd.read_csv(io.StringIO(csv_data))

product_map = {lab: i for i, lab in enumerate(df['产品'].unique())}
quarter_map  = {lab: i for i, lab in enumerate(['Q1','Q2','Q3','Q4'])}
df['x'] = df['产品'].map(product_map)
df['y'] = df['季度'].map(quarter_map)

# ─────────────────── 2. 手搓 3-D 柱子 ────────────────────
bar_w = .4
X, Y, Z, I, J, K, INT = [], [], [], [], [], [], []
offset = 0
for _, r in df.iterrows():
    xc, yc, h = r['x'], r['y'], r['销售额']
    # 8 顶点
    v = [
        (xc-bar_w, yc-bar_w, 0), (xc+bar_w, yc-bar_w, 0),
        (xc+bar_w, yc+bar_w, 0), (xc-bar_w, yc+bar_w, 0),
        (xc-bar_w, yc-bar_w, h), (xc+bar_w, yc-bar_w, h),
        (xc+bar_w, yc+bar_w, h), (xc-bar_w, yc+bar_w, h)
    ]
    for vx, vy, vz in v:
        X.append(vx); Y.append(vy); Z.append(vz); INT.append(h)
    # 12 三角面
    faces = [(0,1,2),(0,2,3),(4,5,6),(4,6,7),
             (0,1,5),(0,5,4),(1,2,6),(1,6,5),
             (2,3,7),(2,7,6),(3,0,4),(3,4,7)]
    for a,b,c in faces:
        I.append(a+offset); J.append(b+offset); K.append(c+offset)
    offset += 8

mesh = go.Mesh3d(
    x=X, y=Y, z=Z, i=I, j=J, k=K,
    intensity=INT, colorscale='Turbo',
    flatshading=True, opacity=1.0,
    colorbar_title='销售额 (万元)',
    lighting=dict(ambient=.3, diffuse=.8, specular=.8,
                  roughness=1.0, fresnel=0.0),
    lightposition=dict(x=100, y=200, z=300)
)

fig = go.Figure(data=[mesh])

# ─────────────────── 3. 轴样式 + 注释标题 ────────────────────
xmax = df['x'].max() + .5
ymax = df['y'].max() + .3
zmax = df['销售额'].max() + 10

axis_style = dict(
    showbackground=True,
    backgroundcolor='#1a1a1a',
    gridcolor='#159', gridwidth=5,
    linecolor='#318',
    tickfont=dict(size=12, color='#AAA'),
    ticklen=6, tickwidth=2, tickcolor='#579',
)

fig.update_layout(
    paper_bgcolor='#000',
    margin=dict(l=0, r=0, t=60, b=0),
    title=dict(text="🚀 产品-季度销售额 (酷炫版)", x=0.5, xanchor='center',
               font=dict(color='#EEE', size=20)),

    scene=dict(
        bgcolor='#111',
        camera=dict(eye=dict(x=1.6, y=1.5, z=0.8)),
        xaxis=dict(**axis_style, tickvals=list(product_map.values()),
                   ticktext=list(product_map.keys())),
        yaxis=dict(**axis_style, tickvals=list(quarter_map.values()),
                   ticktext=list(quarter_map.keys())),
        zaxis=dict(**axis_style),

        annotations=[       # 三段 3-D 注释当标题
            dict(text="产品类型", showarrow=False,
                 x=xmax, y=-.4, z=-5,
                 xanchor='center', yanchor='top',
                 font=dict(color='white', size=14)),
            dict(text="季度", showarrow=False,
                 x=-.4, y=ymax, z=-5,
                 xanchor='center', yanchor='top',
                 font=dict(color='white', size=14)),
            dict(text="销售额 (万元)", showarrow=False,
                 x=-.6, y=-.6, z=zmax,
                 xanchor='left', yanchor='bottom',
                 font=dict(color='white', size=14))
        ]
    )
)

# ─────────────────── 4. 导出 ────────────────────
fig.write_html("interactive_3d_chart_styled.html")
print("✅ 已生成 interactive_3d_chart_styled.html")
