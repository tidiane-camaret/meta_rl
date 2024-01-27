# docs and experiment results can be found at https://docs.cleanrl.dev/rl-algorithms/sac/#sac_continuous_actionpy
import random
import time
import wandb
import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.tensorboard import SummaryWriter

from meta_rl.algorithms.sac.sac_utils import Actor, Args, SoftQNetwork, make_env


def train_sac(env, args: Args = Args(), eval_envs=None):
    import stable_baselines3 as sb3

    if sb3.__version__ < "2.0":
        raise ValueError(
            """Ongoing migration: run the following command to install the new dependencies:
poetry run pip install "stable_baselines3==2.0.0a1"
"""
        )

    run_name = f"{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"
    if args.track:
        import wandb

        wandb.init(
            project=args.wandb_project_name,
            entity=args.wandb_entity,
            sync_tensorboard=True,
            config=vars(args),
            name=run_name,
            monitor_gym=True,
            save_code=True,
        )
    writer = SummaryWriter(f"results/runs/{run_name}")
    writer.add_text(
        "hyperparameters",
        "|param|value|\n|-|-|\n%s"
        % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
    )

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")

    # env setup

    envs = gym.vector.SyncVectorEnv(
        [make_env(env, args.seed, 0, args.capture_video, run_name)]
    )
    assert isinstance(
        envs.single_action_space, gym.spaces.Box
    ), "only continuous action space is supported"

    max_action = float(envs.single_action_space.high[0])

    # JRPL : if context is learned from transitions, use custom replay buffer
    # and create context encoder
    if "learned" in args.context_mode:
        from meta_rl.jrpl.buffer import ReplayBuffer
        from meta_rl.jrpl.context_encoder import ContextEncoder

        latent_context_dim = args.latent_context_dim
        nb_input_transitions = args.nb_input_transitions
        transitions_dim = int(
            2 * np.array(envs.single_observation_space.shape).prod()
            + np.array(envs.single_action_space.shape).prod()
        )
        context_encoder = ContextEncoder(
            d_in=transitions_dim,
            d_out=latent_context_dim,
            hidden_sizes=args.encoder_hidden_sizes,
        ).to(device)
    else:
        from stable_baselines3.common.buffers import ReplayBuffer

        latent_context_dim = 0

    actor = Actor(envs, latent_context_dim).to(device)
    qf1 = SoftQNetwork(envs, latent_context_dim).to(device)
    qf2 = SoftQNetwork(envs, latent_context_dim).to(device)
    qf1_target = SoftQNetwork(envs, latent_context_dim).to(device)
    qf2_target = SoftQNetwork(envs, latent_context_dim).to(device)
    qf1_target.load_state_dict(qf1.state_dict())
    qf2_target.load_state_dict(qf2.state_dict())
    if "learned" in args.context_mode:
        q_optimizer = optim.Adam(
            list(qf1.parameters())
            + list(qf2.parameters())
            + list(context_encoder.parameters()),
            lr=args.q_lr,
        )
    else:
        q_optimizer = optim.Adam(
            list(qf1.parameters()) + list(qf2.parameters()), lr=args.q_lr
        )
    # JRPL : context encoder should be trained with the same optimizer as the actor
    # TODO : see what happens if we train the context encoder with the same optimizer as the critic

    actor_optimizer = optim.Adam(list(actor.parameters()), lr=args.policy_lr)

    # Automatic entropy tuning
    if args.autotune:
        target_entropy = -torch.prod(
            torch.Tensor(envs.single_action_space.shape).to(device)
        ).item()
        log_alpha = torch.zeros(1, requires_grad=True, device=device)
        alpha = log_alpha.exp().item()
        a_optimizer = optim.Adam([log_alpha], lr=args.q_lr)
    else:
        alpha = args.alpha

    envs.single_observation_space.dtype = np.float32
    rb = ReplayBuffer(
        args.buffer_size,
        envs.single_observation_space,
        envs.single_action_space,
        device,
        handle_timeout_termination=False,
    )
    start_time = time.time()

    # TRY NOT TO MODIFY: start the game
    obs, _ = envs.reset(seed=args.seed)
    for global_step in range(args.total_timesteps):
        # ALGO LOGIC: put action logic here
        if global_step < args.learning_starts:
            actions = np.array(
                [envs.single_action_space.sample() for _ in range(envs.num_envs)]
            )
        else:
            # JRPL: to take an action, we first need to encode the context
            if "learned" in args.context_mode:
                context_ids = infos["context_id"]
                # context_id needs to be an int for now. Throw an error if it is not
                # TODO: see if we can avoid the need of context id at all
                """
                if not isinstance(context_ids, int):
                    raise ValueError("context_id should be an int")
                
                data = rb.sample(
                    batch_size=1,
                    context_length=nb_input_transitions,
                    add_context=True,
                    context_id=context_ids,
                )
                """
                contexts = rb.sample_from_context(context_ids, nb_input_transitions).to(
                    device
                )
                context_mu, context_sigma = context_encoder(contexts)
                # TODO : see if we can regularize the model using the std, vae style
                # context_mu = torch.normal(context_mu, context_sigma)
                obs_context = torch.cat(
                    [torch.Tensor(obs).to(device), context_mu], dim=-1
                )
                actions, _, _ = actor.get_action(obs_context)
            else:
                actions, _, _ = actor.get_action(torch.Tensor(obs).to(device))
            actions = actions.detach().cpu().numpy()

        # TRY NOT TO MODIFY: execute the game and log data.
        next_obs, rewards, terminations, truncations, infos = envs.step(actions)
        # TRY NOT TO MODIFY: record rewards for plotting purposes
        if "final_info" in infos:
            for info in infos["final_info"]:
                print(
                    f"global_step={global_step}, episodic_return={info['episode']['r']}"
                )
                writer.add_scalar(
                    "charts/episodic_return", info["episode"]["r"], global_step
                )
                writer.add_scalar(
                    "charts/episodic_length", info["episode"]["l"], global_step
                )
                break

        # TRY NOT TO MODIFY: save data to reply buffer; handle `final_observation`
        real_next_obs = next_obs.copy()
        for idx, trunc in enumerate(truncations):
            if trunc:
                real_next_obs[idx] = infos["final_observation"][idx]

        rb.add(obs, real_next_obs, actions, rewards, terminations, infos)

        # TRY NOT TO MODIFY: CRUCIAL step easy to overlook
        obs = next_obs

        # ALGO LOGIC: training.
        if global_step > args.learning_starts:
            # JRPL : add context latent vector to the observation
            if "learned" in args.context_mode:
                print("step {}/{}".format(global_step, args.total_timesteps))
                # sample contexts from each element of the batch
                """
                data = rb.sample(args.batch_size, nb_input_transitions, add_context=True)
                """
                data = rb.sample(args.batch_size)
                # print(data.context_ids.detach().cpu().numpy())
                contexts = rb.sample_from_context(
                    data.context_ids.detach().cpu().numpy(), nb_input_transitions
                )
                # encode the contexts
                context_mu, context_sigma = context_encoder(contexts.to(device))
                # append the context to the observations
                # TODO : see if we can regularize the model using the std, vae style
                # context_mu = torch.normal(context_mu, context_sigma)
                data = data._replace(
                    observations=torch.cat([data.observations, context_mu], dim=-1)
                )
                data = data._replace(
                    next_observations=torch.cat(
                        [data.next_observations, context_mu.clone().detach()], dim=-1
                    )
                )
            else:
                data = rb.sample(args.batch_size)

            with torch.no_grad():
                next_state_actions, next_state_log_pi, _ = actor.get_action(
                    data.next_observations
                )
                qf1_next_target = qf1_target(data.next_observations, next_state_actions)
                qf2_next_target = qf2_target(data.next_observations, next_state_actions)
                min_qf_next_target = (
                    torch.min(qf1_next_target, qf2_next_target)
                    - alpha * next_state_log_pi
                )
                next_q_value = data.rewards.flatten() + (
                    1 - data.dones.flatten()
                ) * args.gamma * (min_qf_next_target).view(-1)

            qf1_a_values = qf1(data.observations, data.actions).view(-1)
            qf2_a_values = qf2(data.observations, data.actions).view(-1)
            qf1_loss = F.mse_loss(qf1_a_values, next_q_value)
            qf2_loss = F.mse_loss(qf2_a_values, next_q_value)
            qf_loss = qf1_loss + qf2_loss

            # optimize the model
            q_optimizer.zero_grad()
            qf_loss.backward()
            q_optimizer.step()

            if global_step % args.policy_frequency == 0:  # TD 3 Delayed update support
                for _ in range(
                    args.policy_frequency
                ):  # compensate for the delay by doing 'actor_update_interval' instead of 1
                    detached_obs = data.observations.clone().detach()
                    pi, log_pi, _ = actor.get_action(detached_obs)
                    qf1_pi = qf1(detached_obs, pi)
                    qf2_pi = qf2(detached_obs, pi)
                    min_qf_pi = torch.min(qf1_pi, qf2_pi)
                    actor_loss = ((alpha * log_pi) - min_qf_pi).mean()

                    actor_optimizer.zero_grad()
                    actor_loss.backward()
                    actor_optimizer.step()

                    if args.autotune:
                        with torch.no_grad():
                            _, log_pi, _ = actor.get_action(data.observations)
                        alpha_loss = (
                            -log_alpha.exp() * (log_pi + target_entropy)
                        ).mean()

                        a_optimizer.zero_grad()
                        alpha_loss.backward()
                        a_optimizer.step()
                        alpha = log_alpha.exp().item()

            # update the target networks
            if global_step % args.target_network_frequency == 0:
                for param, target_param in zip(
                    qf1.parameters(), qf1_target.parameters()
                ):
                    target_param.data.copy_(
                        args.tau * param.data + (1 - args.tau) * target_param.data
                    )
                for param, target_param in zip(
                    qf2.parameters(), qf2_target.parameters()
                ):
                    target_param.data.copy_(
                        args.tau * param.data + (1 - args.tau) * target_param.data
                    )

            if global_step % 100 == 0:
                writer.add_scalar(
                    "losses/qf1_values", qf1_a_values.mean().item(), global_step
                )
                writer.add_scalar(
                    "losses/qf2_values", qf2_a_values.mean().item(), global_step
                )
                writer.add_scalar("losses/qf1_loss", qf1_loss.item(), global_step)
                writer.add_scalar("losses/qf2_loss", qf2_loss.item(), global_step)
                writer.add_scalar("losses/qf_loss", qf_loss.item() / 2.0, global_step)
                writer.add_scalar("losses/actor_loss", actor_loss.item(), global_step)
                writer.add_scalar("losses/alpha", alpha, global_step)
                print("SPS:", int(global_step / (time.time() - start_time)))
                writer.add_scalar(
                    "charts/SPS",
                    int(global_step / (time.time() - start_time)),
                    global_step,
                )
                if args.autotune:
                    writer.add_scalar(
                        "losses/alpha_loss", alpha_loss.item(), global_step
                    )

    envs.close()

    # plot encoder representations

    # TODO : put this in a separate function with rb and context encoder as arguments
    if "learned" in args.context_mode:
        import matplotlib.pyplot as plt

        context_values = []
        context_embs = []
        # filter out the unique contexts
        contexts_in_rb = rb.hashmap.keys()

        for context_id in contexts_in_rb:
            c = rb.sample_from_context([context_id], nb_input_transitions).to(device)
            context_mu, context_sigma = context_encoder(c)
            context_values.append(args.sampled_contexts[context_id][args.context_name])
            context_embs.append(context_mu.detach().cpu().numpy())

        # plot the context embeddings
        context_values = np.array(context_values)
        context_embs = np.array(context_embs)

        plt.scatter(context_embs[:, 0, 0], context_embs[:, 0, 1], c=context_values)
        plt.colorbar()
        plt.title(
            f"Context embeddings for {args.context_name} using {args.context_mode}"
        )

        plt.savefig(
            f"results/runs/sac_embeddings_{args.env_id}_{args.context_mode}_{args.seed}.png"
        )
        writer.add_figure("charts/context_embeddings", plt.gcf())
    writer.close()

    if eval_envs:
        rewards_list = []
        # evaluate the agent
        for eval_env in eval_envs:
            obs, info = eval_env.reset()
            steps = 0
            rewards = []

            if "learned" not in args.context_mode:
                actions, _, _ = actor.get_action(torch.Tensor(obs).to(device))
                actions = actions.detach().cpu().numpy()
                obs, r, done, truncated, info = env.step(actions)
                steps += 1
                rewards.append(r)
                if done or steps >= args.env_max_episode_steps:
                    break
            else:
                traj_actions = []
                traj_obs = []
                context_mu = torch.zeros(args.latent_context_dim,).to(device)
                context_sigma = torch.zeros(args.latent_context_dim).to(device)
                while True:
                    obs_context = torch.cat(
                        [torch.Tensor(obs).to(device), context_mu], dim=-1
                    )
                    actions, _, _ = actor.get_action(torch.Tensor(obs_context).to(device))
                    actions = actions.detach().cpu().numpy()
                    # add the current transition to the trajectory history
                    traj_actions.append(actions)
                    traj_obs.append(obs)

                    if steps > 1:
                        # transitions should be a tensor of shape [traj_length-1, context_dim]
                        # containing the transitions : (obs, action, next_obs)
                        transitions = np.concatenate(
                            [
                                np.asarray(traj_obs)[:-1],
                                np.asarray(traj_actions).reshape(-1, 1)[:-1],
                                np.asarray(traj_obs)[1:],
                            ],
                            axis=-1,
                        )
                        # if transitions is bigger than context_length, sample a random subset of size context_length
                        if transitions.shape[0] > args.nb_input_transitions:
                            idxs = np.random.randint(
                                0, transitions.shape[0], size=args.nb_input_transitions
                            )
                            transitions = transitions[idxs]
                        # add a dimension for the batch
                        transitions = torch.Tensor(transitions).to(device).unsqueeze(0)
                        context_mu, context_sigma = context_encoder(transitions)
                        # remove the batch dimension
                        context_mu = context_mu.squeeze(0)
                        context_sigma = context_sigma.squeeze(0)

                    obs, r, done, truncated, info = env.step(actions)
                    steps += 1
                    rewards.append(r)

                    if done or steps >= args.env_max_episode_steps:
                        break
            rewards_list.append(np.sum(rewards))

            eval_table = [[x, y] for (x, y) in zip(range(len(rewards_list)), rewards_list)]
            eval_table_wandb = wandb.Table(data=eval_table, columns=["context_value", "reward"])
            writer.log(
                {
                    "eval_table": wandb.plot.line(
                        eval_table_wandb, "context_value", "reward", title="Custom Y vs X Line Plot"
                    )
                }
            )

if __name__ == "__main__":
    train_sac()
