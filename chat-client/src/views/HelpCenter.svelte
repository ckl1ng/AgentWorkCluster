<script>
  import { createEventDispatcher } from 'svelte';

  const dispatch = createEventDispatcher();
  const sections = [
    ['overview', '阅读说明'], ['start', '开始使用'], ['chat', '聊天与群组'],
    ['agent', '云端 Agent'], ['qq', 'QQ Bot 网关'], ['local', '本地执行与 CLI'], ['tools', '工具与记忆'],
    ['approval', '确认与取消'], ['runs', '运行与评估'], ['security', '安全与排错'],
  ];
  const cliCommands = [
    ['auth login [--api URL]', '创建一次性配对会话，等待在网页中批准。默认 API 为 http://127.0.0.1:9011。'],
    ['auth status', '显示已配对的设备 ID 与 API 地址；未配对时显示 authenticated: false。'],
    ['daemon', '启动唯一的本机守护进程，并在 ~/.local-agent/daemon.sock 提供 CLI 通信。'],
    ['status', '读取守护进程版本、工作区、已配置模型和本地运行。守护进程必须已启动。'],
    ['workspace add PATH [--name NAME]', '注册真实目录并同步展示名到控制面；输出本地工作区 ID。'],
    ['workspace list', '列出本地工作区 ID、名称、策略版本和远端工作区 ID，不显示绝对路径。'],
    ['model set AGENT_ID ...', '为一个 Agent 保存本机直连模型凭据，并向服务端登记端点和模型 ID。'],
    ['model list | model remove AGENT_ID', '查看已配置凭据的 Agent，或删除该 Agent 的本机模型凭据。'],
    ['run PROMPT --workspace ID --agent ID', '创建本机文本运行；工作区 ID 使用 workspace list 返回的本地 ID。'],
    ['run list | run events ID | run attach ID', '查看运行、读取事件，或持续输出指定运行的文本并等待终态。'],
  ];

</script>

<section class="help" aria-label="帮助中心">
  <header>
    <div><p>HELP CENTER</p><h2>使用指南</h2><span>聊天、受控 Agent、本地执行器与 QQ Bot 网关</span></div>
    <button type="button" class="close" title="返回消息" aria-label="返回消息" on:click={() => dispatch('close')}>×</button>
  </header>

  <div class="layout">
    <nav aria-label="帮助目录">{#each sections as [id, label]}<a href={`#${id}`}>{label}</a>{/each}</nav>
    <article>
      <section id="overview">
        <p class="eyebrow">OVERVIEW</p><h3>阅读说明</h3>
        <p>本应用包含两条相互隔离的工作流。私聊和群聊在浏览器端加密，服务端仅投递和保存密文。Agent 运行则会把你提交的任务、已授权工具结果和必要上下文发送给 Agent 配置的模型服务，并按 Agent 平台的策略保存运行记录。</p>
        <div class="callout"><strong>先确认执行位置</strong><span>“云端 Agent”由服务端 Worker 执行；“本地执行”由你电脑上的 <code>local-agent</code> daemon 执行。两者的凭据、工作区和故障排查路径不同。</span></div>
      </section>

      <section id="start">
        <p class="eyebrow">GET STARTED</p><h3>开始使用</h3>
        <ol>
          <li>登录后，从左侧选择已有的私聊、群聊或 Agent；顶部侧栏按钮可收起导航以获得更大阅读区域。</li>
          <li>通过左侧“管理”搜索用户并发送好友请求。对方接受后，双方才会出现在联系人列表。</li>
          <li>需要处理任务时，在 Agent 区域点加号创建 Agent。保存后进入它的独立会话，再发送一项明确、可验收的任务。</li>
          <li>需要使用本机模型或把运行交给自己的电脑时，先完成“本地执行与 CLI”章节的设备配对与工作区注册。</li>
        </ol>
      </section>

      <section id="chat">
        <p class="eyebrow">MESSAGING</p><h3>聊天、联系人与群组</h3>
        <h4>私聊</h4><p>从联系人条目进入私聊。头像旁的状态用于提示在线情况；对方离线时，消息会在其下次连接后获取。未读角标表示该会话有尚未查看的新消息。</p>
        <h4>群聊</h4><p>先在“管理”中创建群组，填写名称并至少选择一位联系人。客户端会为每位成员加密分发独立群密钥。不要分享浏览器存储、私钥、群密钥或其截图。</p>
        <h4>联系人</h4><ul><li>搜索用户名至少输入两个字符。</li><li>好友请求需要由对方在“好友请求”区域接受。</li><li>创建群组成功后会直接打开新的群聊。</li></ul>
      </section>

      <section id="agent">
        <p class="eyebrow">CLOUD AGENT</p><h3>创建与配置 Agent</h3>
        <p>Agent 是独立任务会话，不会自动读取你的私聊或群聊。创建时填写名称、用途、模型连接、API Key、系统提示词以及运行预算。API Key 加密保存在服务端，保存后不会回显到浏览器。</p>
        <h4>基础配置</h4>
        <ul><li><strong>模型连接：</strong>模型 ID、Base URL、温度、最大输出 Token 和超时。更新时 API Key 留空会保留已有密钥。</li><li><strong>上下文与预算：</strong>上下文窗口、并发运行、单次运行工具调用上限，以及每日和每月 Token 上限；预算填 <code>0</code> 表示不限制。</li><li><strong>运行状态：</strong>暂停后不能继续创建新运行；恢复后才能再次运行。</li></ul>
        <p>保存后在 Agent 会话中输入任务。运行以流式事件呈现；不要把模型的任何中间内容视为对外部系统已经完成的操作，应以最终回复、运行记录和工具结果为准。</p>
      </section>

      <section id="qq">
        <p class="eyebrow">QQ BOT GATEWAY</p><h3>接入 QQ Bot</h3>
        <p>QQ Bot 网关将 QQ 的群聊 @ 消息和私聊消息转成当前 Agent 的独立运行，再将最终回复回传 QQ。Gateway 只处理 QQ 平台连接、事件去重、会话映射和消息发送；Agent、Conversation、Run、工具和模型调用仍由 Agent 服务负责。</p>
        <div class="callout"><strong>先准备目标 Agent</strong><span>先在本应用创建一个可正常运行的 Agent，记录它的 Agent ID 和所属账号的用户 ID。网关会把每个 QQ 群或私聊范围映射到该 Agent 的独立会话。</span></div>

        <h4>配置服务端</h4>
        <div class="callout"><strong>推荐：网页一键连接</strong><span>AppID、AppSecret、Bot ID 和目标 Agent 不需要写入 `.env`。打开目标 Agent 的“配置”，在 QQ Bot 连接区域填写 AppID/AppSecret 后点击“一键连接 QQ Bot”；AppSecret 由 Gateway 加密保存。</span></div>
        <ol><li>在 QQ 开放平台创建机器人应用，取得 AppID 和 AppSecret，并确认当前官方文档要求的事件订阅权限。</li><li>在部署机器的 <code>chat-server/.agent.env</code> 只配置服务端密钥；不要把 AppSecret 写入文件。</li><li>将 <code>QQ_GATEWAY_ENABLED</code> 设为 <code>true</code> 启动 Gateway，然后在目标 Agent 的“配置 → QQ Bot 连接”中填写 AppID/AppSecret。</li></ol>
        <pre><code>QQ_GATEWAY_ENABLED=true
## AppID/AppSecret 在网页 Agent 设置中填写，不放入环境文件
QQ_GATEWAY_MASTER_KEY=your-fernet-key</code></pre>
        <p>使用以下命令生成 <code>QQ_GATEWAY_MASTER_KEY</code>，并将生成值仅保存到部署环境：<code>python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'</code>。</p>

        <h4>QQ 接入方式</h4>
        <p>网页一键连接仅使用 WebSocket；不需要配置回调地址，也不需要向公网暴露 <code>/qq/webhook*</code>。事件 Intents 默认使用 <code>33554432</code>（<code>GROUP_AND_C2C_EVENT</code>），用于接收群聊 @ 和私聊消息。</p>

        <h4>启动与验证</h4>
        <pre><code>cd /home/zhouzw/AgentWorkCluster
./start.sh
curl http://127.0.0.1:9013/healthz</code></pre>
        <p>健康检查返回 <code>&#123;&quot;status&quot;:&quot;ok&quot;,&quot;configured&quot;:true&#125;</code> 后，再在 QQ 群中 @ 机器人或向机器人发送私聊消息。首次消息会创建会话，后续同一群或同一用户的消息复用该会话。</p>

        <h4>运行与故障排查</h4>
        <ul><li><strong>未配置时不启动：</strong>默认 <code>QQ_GATEWAY_ENABLED=false</code>，不会影响普通聊天、Agent 或前端启动。</li><li><strong>被动回复时限：</strong>QQ 事件超过被动回复窗口后会标记为过期，不会使用失效的消息 ID 重发。模型或工具耗时应保持在该时限内。</li><li><strong>重复消息：</strong>Gateway 按机器人和事件 ID 去重，Agent 服务也会保存相同的幂等键，避免重试产生重复运行或回复。</li><li><strong>Docker 部署：</strong>填写完整环境变量后使用 <code>docker compose --profile qq up -d</code> 启动可选的 QQ Gateway 服务。</li></ul>
      </section>

      <section id="local">
        <p class="eyebrow">LOCAL AGENT</p><h3>本地执行与项目 CLI</h3>
        <p><code>local-agent</code> 是位于服务端项目 <code>local-agent/</code> 的 Node.js daemon/CLI。daemon 是同一台电脑上的唯一执行进程：网页创建的本地运行和终端创建的运行都通过它管理。本地目录绝对路径与模型 API Key 不会上传到 Agent API。</p>
        <div class="callout warning"><strong>当前范围</strong><span>当前 Local Agent 只执行文本模型运行。它不提供本机文件编辑、进程执行、终端工具调用、工具确认或断线恢复；不要将其作为生产远程执行器部署。</span></div>

        <h4>部署 daemon</h4>
        <p>在需要执行任务的电脑部署 CLI，不要将它放进 Agent API 容器。该电脑需要 Node.js 18+，并且能访问对外网关。网关必须转发 <code>/api/v1/local-agent*</code> 与 <code>/local-agent/ws*</code>；生产环境使用网关 URL，不要向外暴露内部 Agent API 的 <code>9011</code> 端口。</p>
        <pre><code>cd /home/zhouzw/agentWorkCluster/local-agent
npm ci
node bin/local-agent.js daemon</code></pre>
        <p>上面的方式适合临时启动。生产环境可用当前用户的 systemd service 保持 daemon 常驻。创建 <code>~/.config/systemd/user/local-agent.service</code>，将路径替换为实际安装目录，并保持数据目录属于运行该服务的用户：</p>
        <pre><code>[Unit]
Description=Local Agent daemon
After=network-online.target

[Service]
WorkingDirectory=/opt/agentWorkCluster/local-agent
ExecStart=/usr/bin/node /opt/agentWorkCluster/local-agent/bin/local-agent.js daemon
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
</code></pre>
        <p>保存后执行：</p>
        <pre><code>systemctl --user daemon-reload
systemctl --user enable --now local-agent
loginctl enable-linger "$USER"
systemctl --user status local-agent</code></pre>
        <p>daemon 在 <code>~/.local-agent/</code> 保存凭据、工作区、模型凭据、journal 和 IPC socket；目录及文件必须仅当前用户可读。每个用户只能运行一个 daemon。</p>

        <h4>首次配对、批准与工作区注册</h4>
        <ol><li>在另一个终端创建配对会话。开发环境可省略 <code>--api</code> 并使用默认 <code>http://127.0.0.1:9011</code>；部署环境应传入可从该电脑访问的网关地址，例如 <code>https://chat.example.com</code> 或 <code>http://host:9010</code>。</li><li>命令会输出“配对会话 ID”和“配对码”。在网页打开任一 Agent 的“配置 → 本地执行”，填入两项并点“批准本地设备”。</li><li>配对成功后，注册你明确授权的目录。命令输出的 <code>id</code> 是终端运行使用的本地工作区 ID；网页只保存同步后的展示名和远端 ID。</li></ol>
        <pre><code>node bin/local-agent.js auth login --api https://chat.example.com
node bin/local-agent.js auth status
node bin/local-agent.js workspace add /absolute/path/to/project --name my-project
node bin/local-agent.js workspace list</code></pre>
        <p>工作区注册会解析真实目录并拒绝不存在的路径。服务端不会保存绝对路径；<code>workspace list</code> 也不会显示它。未配对时可先添加本地工作区，完成配对后重新执行添加命令以同步控制面。</p>

        <h4>在网页绑定本地 Agent</h4>
        <p>在“配置 → 本地执行”选择已批准的设备和已同步的工作区，然后选择模型模式并点击“绑定本地执行”。<code>local_direct</code> 要求在本机为该 Agent 配置模型凭据，并且是当前唯一会被服务端派发给 daemon 的本地执行模式。界面仍提供 <code>server_proxy</code>（使用服务端已有模型连接）作为控制面选项，但当前版本不会将该模式的本地运行派发给 daemon；要实际运行请使用 <code>local_direct</code>。</p>

        <h4>配置本机直连模型</h4>
        <p>先完成配对并保持 daemon 运行。将 API Key 放在环境变量中，避免它出现在 shell 历史中；默认变量名是 <code>LOCAL_AGENT_MODEL_API_KEY</code>，可用 <code>--api-key-env</code> 指定其他变量名。</p>
        <pre><code>export LOCAL_AGENT_MODEL_API_KEY='your-model-key'
node bin/local-agent.js model set AGENT_ID \
  --base-url https://model.example/v1 \
  --model-id model-name
node bin/local-agent.js model list</code></pre>
        <p>CLI 会在本机加密保存 API Key，仅向服务端登记 Base URL 与模型 ID。执行 <code>model remove AGENT_ID</code> 会移除该 Agent 的本机模型凭据。</p>
        <div class="callout warning"><strong>新建纯本地 Agent 的当前限制</strong><span><code>local_direct</code> 要求 Agent 从未保存服务端模型 Key，但现有“创建 Agent”表单仍要求填写 API Key。因此，新建纯本地 Agent 目前需要通过 Agent API 以无 API Key、<code>execution_target=local</code>、<code>model_mode=local_direct</code> 创建；已经保存服务端 Key 的 Agent 不能改绑为 <code>local_direct</code>。</span></div>

        <h4>从终端创建和观察运行</h4>
        <p>终端运行使用 <code>workspace list</code> 输出的本地工作区 ID，而不是网页绑定时看到的远端工作区 ID。<code>run</code> 需要已配对设备、运行中的 daemon，以及该 Agent 的本机模型凭据。</p>
        <pre><code>node bin/local-agent.js run "总结当前变更" \
  --workspace ws_xxx \
  --agent AGENT_ID
node bin/local-agent.js run list
node bin/local-agent.js run events run_xxx
node bin/local-agent.js run attach run_xxx</code></pre>
        <p><code>run attach</code> 会持续打印文本事件，并在运行完成、失败、取消或找不到记录时退出。当前 CLI 没有单独的取消命令。终端直接创建的运行保留在本机 journal；网页只能取消通过网页 Agent 会话创建并已派发的本地运行。</p>

        <h4>CLI 命令速查</h4>
        <div class="table-wrap"><table><thead><tr><th>命令</th><th>用途</th></tr></thead><tbody>{#each cliCommands as [command, description]}<tr><td><code>local-agent {command}</code></td><td>{description}</td></tr>{/each}</tbody></table></div>
      </section>

      <section id="tools">
        <p class="eyebrow">GOVERNANCE</p><h3>工具与长期记忆</h3>
        <p>工具不会因为创建而自动提供给模型。只有在“配置 → 工具与记忆”中分配给当前 Agent 的工具，才会进入后续运行快照；预设工具也可以按 Agent 单独启用或取消。</p>
        <h4>创建工具</h4>
        <ul><li><strong>HTTP：</strong>填写 URL、方法、Headers、参数位置和输入 JSON Schema。只有 <code>GET</code>/<code>HEAD</code> 才适合 <code>read</code>。</li><li><strong>远程 MCP：</strong>填写 MCP 服务 URL 和远程工具名。发现结果只是候选，审阅后仍要确认副作用、确认策略、Schema 和限额。</li><li><strong>本地命令与 MCP STDIO：</strong>使用命令及 JSON 字符串数组参数。它们受既有工具策略约束，且与当前 Local Agent 的文本运行能力不同。</li><li><strong>副作用与确认：</strong><code>write</code> 必须选择“每运行确认”或“每次确认”；<code>destructive</code> 强制“每次确认”。每运行限额限制单次任务可调用次数。</li></ul>
        <h4>长期记忆</h4><p>长期记忆默认关闭。启用后，创建者可保存事实、偏好、档案、约束和经验，并设定 0 到 100 的重要度。只保存简短、可验证且会长期复用的信息；冲突项标为 <code>conflicted</code>，不会静默覆盖，应人工删除错误项。</p>
      </section>

      <section id="approval">
        <p class="eyebrow">CONFIRMATION</p><h3>确认、拒绝与取消</h3>
        <p>当运行请求写入或破坏性工具时，聊天底部会显示确认面板，包含工具名、副作用级别和脱敏参数。确认只绑定当前运行、当前工具调用和当前参数哈希；参数改变后会重新请求确认。</p>
        <ul><li><strong>批准：</strong>允许满足当前确认内容的调用继续执行。</li><li><strong>拒绝：</strong>当前运行结束，不调用外部系统。</li><li><strong>取消：</strong>停止排队、运行中或等待确认的任务。不要用刷新页面代替取消。</li></ul>
        <p>确认后，运行会从已保存检查点继续，不会重新执行已记录的外部副作用。</p>
      </section>

      <section id="runs">
        <p class="eyebrow">RUN WORKBENCH</p><h3>运行记录与评估</h3>
        <p>从 Agent 会话工具栏打开“运行记录”。左侧按状态筛选并选择一条运行；右侧显示最终回复、Token 用量、尝试次数、工具确认和脱敏审计时间线。本地派发的运行还会显示“等待本机”“等待领取”或“本机执行中”等状态与工作区 ID。</p>
        <h4>评估对比</h4><p>在底部选择基线和候选评估运行后点击“比较”。出现回归 case 时，候选版本不应视为可发布；先检查模型、Harness、工具、策略或提示词变化，再重新评估。</p>
      </section>

      <section id="security">
        <p class="eyebrow">SECURITY &amp; TROUBLESHOOTING</p><h3>安全与故障排查</h3>
        <h4>必须遵守的边界</h4>
        <ul><li>不要在聊天、提示词、工具描述、记忆、日志或截图中粘贴 API Key、Cookie、Bearer token、私钥、群密钥、QQ AppSecret 或 Gateway 加密主密钥。</li><li>只把确实需要的工具分配给 Agent；未知 MCP 工具默认按写入处理并逐次确认。</li><li>本地工作区应只注册需要授权的项目目录。Local Agent 目前不会执行文件或进程工具，不要据此推断未来版本也会有相同默认权限。</li><li>工具网络访问受服务端策略限制；不要尝试通过 URL、重定向或 DNS 绕过私网和本地地址限制。</li></ul>
        <h4>常见问题</h4>
        <dl><dt><code>connect ENOENT ... daemon.sock</code></dt><dd>daemon 未启动或已退出。回到 <code>local-agent</code> 目录执行 <code>node bin/local-agent.js daemon</code>，并保持该进程运行。</dd><dt><code>local-agent daemon is already running</code></dt><dd>同一用户已有 daemon。使用已有实例；若它刚被异常中断，先确认原进程已停止，再重新启动。</dd><dt>配对码过期或网页批准失败</dt><dd>重新运行 <code>auth login</code> 获取新的会话 ID 和配对码，并确认网页登录的是要拥有该设备的账号。</dd><dt><code>workspace not found</code></dt><dd>终端运行必须使用 <code>workspace list</code> 返回的本地 <code>ws_...</code> ID，而不是网页中的远端工作区 ID。</dd><dt><code>no local model credential is configured</code></dt><dd>为相同 Agent ID 执行 <code>model set</code>，并确认配对、daemon 和环境变量均已就绪。</dd><dt>网页运行一直等待本机</dt><dd>检查设备在“本地执行”页是否在线、daemon 是否仍在运行、绑定的工作区是否已同步，并确认该 Agent 绑定为 <code>local_direct</code>。</dd><dt>QQ Gateway 健康检查未配置或启动失败</dt><dd>确认 <code>QQ_GATEWAY_ENABLED=true</code>、所有 <code>QQ_*</code> 变量、目标 Agent ID 和所属用户 ID 已填写；随后查看 <code>qq-gateway/.runtime/qq-gateway.log</code>。不要将其中的凭证复制到工单或聊天。</dd></dl>
      </section>
    </article>
  </div>
</section>

<style>
  .help { flex:1; min-width:0; overflow:auto; padding:28px max(20px,6vw) 52px; }.help > header,.layout { max-width:1120px; }.help > header { display:flex; justify-content:space-between; gap:20px; margin-bottom:22px; }.help header p,.eyebrow { margin:0; color:var(--color-primary); font-size:10px; font-weight:700; letter-spacing:1px; }.help h2 { margin:4px 0; font-size:24px; }.help header span { color:var(--color-text-muted); font-size:13px; }.close { display:grid; width:36px; height:36px; place-items:center; flex:0 0 auto; padding:0; border:1px solid var(--color-border); border-radius:4px; background:transparent; color:var(--color-text-muted); cursor:pointer; font-size:24px; }.layout { display:grid; grid-template-columns:190px minmax(0,1fr); align-items:start; gap:34px; }.layout nav { position:sticky; top:0; display:grid; gap:2px; padding:8px; border:1px solid var(--color-border); border-radius:5px; background:var(--color-surface); }.layout nav a { padding:8px; border-radius:3px; color:var(--color-text-muted); font-size:12px; text-decoration:none; }.layout nav a:hover { background:var(--color-hover); color:var(--color-primary); }article { min-width:0; }article section { scroll-margin-top:20px; padding:0 0 26px; margin-bottom:24px; border-bottom:1px solid var(--color-border); }article h3 { margin:5px 0 10px; font-size:18px; }article h4 { margin:18px 0 7px; font-size:14px; }article p,article li,article dd,table { color:var(--color-text-muted); font-size:14px; line-height:1.7; }article ol,article ul { margin:10px 0 0 22px; }article li + li { margin-top:5px; }article strong { color:var(--color-text); }code { overflow-wrap:anywhere; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; }.callout { display:grid; gap:4px; margin-top:15px; padding:11px 13px; border-left:3px solid var(--color-primary); background:var(--color-active); color:var(--color-text-muted); font-size:13px; line-height:1.6; }.callout.warning { border-left-color:var(--color-error); }.callout strong { font-size:13px; }.callout span { color:var(--color-text-muted); }.qq-link-panel { display:grid; gap:10px; margin-top:15px; padding:15px; border:1px solid var(--color-border); border-radius:6px; background:var(--color-surface); }.qq-link-panel h4 { margin:0 0 3px; }.qq-link-panel p { margin:0; font-size:13px; }.qq-link-panel label { display:grid; gap:6px; color:var(--color-text-muted); font-size:12px; font-weight:600; }.qq-link-panel input { width:100%; min-width:0; padding:9px 10px; border:1px solid var(--color-border); border-radius:5px; background:var(--color-input); color:var(--color-text); font:inherit; font-size:13px; outline:none; }.qq-link-panel input:focus { border-color:var(--color-primary); }.qq-link-actions { display:flex; flex-wrap:wrap; gap:8px; }.qq-link-actions button { display:inline-flex; align-items:center; gap:6px; min-height:34px; padding:0 11px; border:1px solid var(--color-primary); border-radius:4px; background:var(--color-primary); color:#fff; cursor:pointer; font-size:12px; font-weight:700; }.qq-link-actions button.secondary { border-color:var(--color-border); background:transparent; color:var(--color-text-muted); }.qq-link-actions button.danger { border-color:var(--color-error); background:transparent; color:var(--color-error); }.qq-link-actions button:disabled { opacity:.5; cursor:default; }.qq-link-feedback.error { color:var(--color-error); }.qq-link-feedback.success { color:var(--color-online); }.help pre { overflow:auto; margin:11px 0 0; padding:12px 14px; border:1px solid var(--color-border); border-radius:5px; background:var(--color-input); color:var(--color-text); font-size:12px; line-height:1.65; }.help pre code { white-space:pre; overflow-wrap:normal; }.table-wrap { overflow-x:auto; margin-top:12px; border:1px solid var(--color-border); border-radius:5px; }.table-wrap table { width:100%; min-width:620px; border-collapse:collapse; text-align:left; }.table-wrap th,.table-wrap td { padding:9px 11px; border-bottom:1px solid var(--color-border); vertical-align:top; }.table-wrap th { color:var(--color-text); font-size:12px; }.table-wrap tr:last-child td { border-bottom:0; }.table-wrap td:first-child { width:38%; color:var(--color-text); }.help dl { margin:10px 0 0; }.help dt { margin-top:13px; color:var(--color-text); font-size:13px; font-weight:700; }.help dd { margin:4px 0 0; }@media(max-width:760px){.help{padding:22px 16px 40px}.layout{grid-template-columns:1fr;gap:18px}.layout nav{position:static;display:flex;overflow:auto;white-space:nowrap}.layout nav a{flex:0 0 auto}.help h2{font-size:21px}.help pre{font-size:11px}.qq-link-actions button{flex:1 1 auto}}
</style>
