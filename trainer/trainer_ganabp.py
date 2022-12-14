import os
import pdb
import cv2
import time
import torch
import torch.nn.functional as F
from torch.autograd import Variable
from tqdm import tqdm
from config import param as option
from utils import AvgMeter, label_edge_prediction, visualize_list, make_dis_label
from loss.get_loss import cal_loss
from utils import DotDict
if torch.cuda.is_available():#############################3
  device = torch.device("cuda")
else:
  device = torch.device("cpu")

CE = torch.nn.BCELoss()
def train_one_epoch(epoch, model_list, optimizer_list, train_loader, dataset_size, loss_fun):
    ## Setup abp params
    opt = DotDict()
    opt.latent_dim = option['ganabp_config']['latent_dim']
    opt.langevin_step_num_gen = option['ganabp_config']['step_num']
    opt.sigma_gen = option['ganabp_config']['sigma_gen']
    opt.langevin_s = option['ganabp_config']['langevin_s']
    opt.pred_label = option['ganabp_config']['pred_label']
    opt.gt_label = option['ganabp_config']['gt_label']
    opt.lamda_dis = option['ganabp_config']['lamda_dis']
    train_z = torch.FloatTensor(dataset_size, opt.latent_dim).normal_(0, 1).to(device)
    ## Setup abp params

    generator, discriminator = model_list
    generator_optimizer, discriminator_optimizer = optimizer_list
    generator.train()
    if discriminator is not None:
        discriminator.train()
    loss_record, supervised_loss_record, dis_loss_record = AvgMeter(), AvgMeter(), AvgMeter()
    print('Learning Rate: {:.2e}'.format(generator_optimizer.param_groups[0]['lr']))
    progress_bar = tqdm(train_loader, desc='Epoch[{:03d}/{:03d}]'.format(epoch, option['epoch']))
    for i, pack in enumerate(progress_bar):
        for rate in option['size_rates']:
            generator_optimizer.zero_grad()
            if discriminator is not None:
                discriminator_optimizer.zero_grad()
            if len(pack) == 3:
                images, gts, depth, index = pack['image'].to(device), pack['gt'].to(device), None, pack['index']
            elif len(pack) == 4:
                images, gts, depth, index = pack['image'].to(device), pack['gt'].to(device), pack['depth'].to(device), pack['index']
            elif len(pack) == 5:
                images, gts, mask, gray, depth = pack['image'].to(device), pack['gt'].to(device), pack['mask'].to(device), pack['gray'].to(device), None

            # multi-scale training samples
            trainsize = (int(round(option['trainsize'] * rate / 32) * 32), int(round(option['trainsize'] * rate / 32) * 32))
            if rate != 1:
                images = F.upsample(images, size=trainsize, mode='bilinear', align_corners=True)
                gts = F.upsample(gts, size=trainsize, mode='bilinear', align_corners=True)

            z_noise = torch.randn(images.shape[0], opt.latent_dim).to(images.device)
            z_noise = Variable(z_noise, requires_grad=True)
            z_noise_preds = [z_noise.clone() for _ in range(opt.langevin_step_num_gen + 1)]
            for kk in range(opt.langevin_step_num_gen):
                z_noise = Variable(z_noise_preds[kk], requires_grad=True).to(device)
                noise = torch.randn(z_noise.size()).to(device)

                gen_res = generator(img=images, z=z_noise, depth=depth)['sal_pre']
                gen_loss = 0
                for i in gen_res:
                    if option['task'].lower() == 'weak-rgb-sod':
                        gen_loss += 1 / (2.0 * opt.sigma_gen * opt.sigma_gen) * F.mse_loss(torch.sigmoid(i)*mask, gts*mask, size_average=True, reduction='sum')
                    else:
                        gen_loss += 1 / (2.0 * opt.sigma_gen * opt.sigma_gen) * F.mse_loss(torch.sigmoid(i), gts, size_average=True, reduction='sum')
                gen_loss.backward(torch.ones(gen_loss.size()).to(device))

                grad = z_noise.grad
                z_noise = z_noise + 0.5 * opt.langevin_s * opt.langevin_s * grad
                z_noise += opt.langevin_s * noise
                z_noise_preds[kk + 1] = z_noise

            z_noise_post = z_noise_preds[-1]
            pred_post = generator(img=images, z=z_noise_post, depth=depth)['sal_pre']

            if option['task'].lower() == 'sod':
                Dis_output = discriminator(torch.cat((images, torch.sigmoid(pred_post[0]).detach()), 1))
            elif option['task'].lower() == 'weak-rgb-sod':
                Dis_output = discriminator(torch.cat((images, mask*torch.sigmoid(pred_post[0]).detach()), 1))
            elif option['task'].lower() == 'cod':#################
                Dis_output = discriminator(torch.cat((images, torch.sigmoid(pred_post[0]).detach()), 1))

            up_size = (images.shape[2], images.shape[3])
            Dis_output = F.upsample(Dis_output, size=up_size, mode='bilinear', align_corners=True)
            
            loss_dis_output = CE(torch.sigmoid(Dis_output), make_dis_label(opt.gt_label, gts))
            if (option['task'].lower() == 'sod') or (option['task'].lower() == 'cod'):#####################3
                supervised_loss = cal_loss(pred_post, gts, loss_fun)
            elif option['task'].lower() == 'weak-rgb-sod':
                supervised_loss = loss_fun(images=images, outputs=pred_post, gt=gts, masks=mask, grays=gray, model=generator)
            loss_all = supervised_loss + opt.lamda_dis * loss_dis_output
            loss_all.backward()
            generator_optimizer.step()

            # train discriminator
            dis_pred = torch.sigmoid(pred_post[0]).detach()
            if (option['task'].lower() == 'sod') or (option['task'].lower() == 'cod'):#####################3
                Dis_output = discriminator(torch.cat((images, dis_pred), 1))
            elif option['task'].lower() == 'weak-rgb-sod':
                Dis_output = discriminator(torch.cat((images, mask*dis_pred), 1))

            Dis_target = discriminator(torch.cat((images, gts), 1))
            Dis_output = F.upsample(torch.sigmoid(Dis_output), size=up_size, mode='bilinear', align_corners=True)
            Dis_target = F.upsample(torch.sigmoid(Dis_target), size=up_size, mode='bilinear', align_corners=True)

            loss_dis_output = CE(torch.sigmoid(Dis_output), make_dis_label(opt.pred_label, gts))
            loss_dis_target = CE(torch.sigmoid(Dis_target), make_dis_label(opt.gt_label, gts))
            dis_loss = 0.5 * (loss_dis_output + loss_dis_target)
            dis_loss.backward()
            discriminator_optimizer.step()

            result_list = [torch.sigmoid(x) for x in pred_post]
            result_list.append(gts)
            result_list.append(Dis_output)
            result_list.append(Dis_target)
            visualize_list(result_list, option['log_path'])

            if rate == 1:
                loss_record.update(supervised_loss.data, option['batch_size'])
                supervised_loss_record.update(loss_all.data, option['batch_size'])
                dis_loss_record.update(dis_loss.data, option['batch_size'])

        progress_bar.set_postfix(loss=f'{loss_record.show():.3f}|{supervised_loss_record.show():.3f}|{dis_loss_record.show():.3f}')

    return {'generator': generator, "discriminator": discriminator}, loss_record
