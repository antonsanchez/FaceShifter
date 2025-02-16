from network.AEI_Net import *
from network.MultiscaleDiscriminator import *
from utils.Dataset import FaceEmbed, With_Identity
from torch.utils.data import DataLoader
import torch.optim as optim
from face_modules.model import Backbone, Arcface, MobileFaceNet, Am_softmax, l2_norm
import torch.nn.functional as F
import torch
import time
import torchvision
import cv2
#from apex import amp
#import visdom
from torch.utils.tensorboard import SummaryWriter
from DiffAugment_pytorch import DiffAugment
import pickle
from torch.cuda.amp import autocast, GradScaler

#vis = visdom.Visdom(server='127.0.0.1', env='faceshifter', port=8099)
batch_size = 9
lr_G = 1e-4
lr_D = 4e-4
max_epoch = 2000
show_step = 50
save_epoch = 1
model_save_path = './saved_models/'
optim_level = 'O1'
policy = 'color'
min_iter = 0
max_iter = 900000

device = torch.device('cuda')

G = AEI_Net(c_id=512).to(device)
#mynorm = lambda x: torch.nn.GroupNorm(x // 16, x)
#G = AEI_Net(c_id=512, norm=mynorm).to(device)
D = MultiscaleDiscriminator(input_nc=3, n_layers=6, norm_layer=torch.nn.InstanceNorm2d).to(device)
G.train()
D.train()

arcface = Backbone(50, 0.6, 'ir_se').to(device)
arcface.eval()
arcface.load_state_dict(torch.load('./face_modules/model_ir_se50.pth', map_location=device), strict=False)
arcface.requires_grad_(False)

opt_G = optim.Adam(G.parameters(), lr=lr_G, betas=(0, 0.999))
opt_D = optim.Adam(D.parameters(), lr=lr_D, betas=(0, 0.999))

scaler = GradScaler()

try:
    G.load_state_dict(torch.load('./saved_models/G_latest.pth', map_location=torch.device('cpu')), strict=False)
    D.load_state_dict(torch.load('./saved_models/D_latest.pth', map_location=torch.device('cpu')), strict=False)
    opt_G.load_state_dict(torch.load('./saved_models/optG_latest.pth', map_location=torch.device('cpu')))
    opt_D.load_state_dict(torch.load('./saved_models/optD_latest.pth', map_location=torch.device('cpu')))
    scaler.load_state_dict(torch.load('./saved_models/scaler_latest.pth', map_location=torch.device('cpu')))
except Exception as e:
    print(e)
try:
    with open('./saved_models/niter.pkl', 'rb') as f:
        min_iter = pickle.load(f)
except Exception as e:
    print(e)
writer = SummaryWriter('runs/FaceShifter', purge_step=min_iter)

TrainFaceSources = ['/home/olivier/Images/FaceShifter/celeba-256/', '/home/olivier/Images/FaceShifter/Perso/', '/home/olivier/Images/FaceShifter/VGGFaceTrain/', '/home/olivier/Images/FaceShifter/FFHQ/']
train_dataset = FaceEmbed(TrainFaceSources, same_prob=0.2)

train_dataloader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, num_workers=4, pin_memory=True, drop_last=True)
train_loader = iter(train_dataloader)


MSE = torch.nn.MSELoss()
L1 = torch.nn.L1Loss()


def hinge_loss(X, positive=True):
    if positive:
        return torch.relu(1-X).mean()
    else:
        return torch.relu(X+1).mean()

def get_grid_image(X):
    X = X[:8]
    X = torchvision.utils.make_grid(X.detach().cpu(), nrow=X.shape[0]) * 0.5 + 0.5
    return X


def make_image(Xs, Xt, Y):
    Xs = get_grid_image(Xs)
    Xt = get_grid_image(Xt)
    Y = get_grid_image(Y)
    return torch.cat((Xs, Xt, Y), dim=1).numpy()

print(torch.backends.cudnn.benchmark)
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.enabled = True
for niter in range(min_iter, max_iter):
    # torch.cuda.empty_cache()
    start_time = time.time()
    epoch = niter // len(train_dataloader)
    iteration = niter % len(train_dataloader)
    try:
        Xs, Xt, same_person = next(train_loader)
    except (OSError, StopIteration):
        train_loader = iter(train_dataloader)
        Xs, Xt, same_person = next(train_loader)
    Xs = Xs.to(device)
    Xt = Xt.to(device)
    # embed = embed.to(device)
    with torch.no_grad():
        embed, _ = arcface(F.interpolate(Xs[:, :, 19:237, 19:237], [112, 112], mode='bilinear', align_corners=True))
    same_person = same_person.to(device)
    Xt.requires_grad = True
    embed.requires_grad = True

    # train G
    D.requires_grad_(False)
    opt_G.zero_grad()
    with autocast():
        Y, Xt_attr = G(Xt, embed)

        Di = D(DiffAugment(Y, policy=policy))
        L_adv = 0

        for di in Di:
            #L_adv += hinge_loss(di[0], True)
            L_adv -= di[0].mean()
        L_adv /= len(Di)

        Y_aligned = Y[:, :, 19:237, 19:237]
        ZY, _ = arcface(F.interpolate(Y_aligned, [112, 112], mode='bilinear', align_corners=True))
        L_id =(1 - torch.cosine_similarity(embed, ZY, dim=1)).mean()

        Y_attr = G.get_attr(Y)
        L_attr = 0
        for i in range(len(Xt_attr)):
            #L_attr += torch.mean(torch.pow(Xt_attr[i] - Y_attr[i], 2).reshape(batch_size, -1), dim=1).mean()
            L_attr += torch.mean(torch.pow(Xt_attr[i] - Y_attr[i], 2))
        L_attr /= 2.0

        #L_rec = torch.sum(0.5 * torch.mean(torch.pow(Y - Xt, 2).reshape(batch_size, -1), dim=1) * same_person) / (same_person.sum() + 1e-6)
        L_rec = MSE(Y[same_person], Xt[same_person]) * same_person.sum() /(2.0 * batch_size)

        lossG = 1*L_adv + 10*L_attr + 5*L_id + 10*L_rec

    scaler.scale(lossG).backward()
    scaler.step(opt_G)

    # train D
    D.requires_grad_(True)
    opt_D.zero_grad()
    Xf = Y.detach()
    Xs.requires_grad = True
    Xf.requires_grad = True
    with autocast():
        fake_D = D(DiffAugment(Xf, policy=policy))
        loss_fake = 0
        for di in fake_D:
            loss_fake += hinge_loss(di[0], False)
        loss_fake /= len(fake_D)

        true_D = D(DiffAugment(Xs, policy=policy))
        loss_true = 0
        for di in true_D:
            loss_true += hinge_loss(di[0], True)
        loss_true /= len(true_D)

        #lossD = (loss_true + loss_fake) / 2.0
        lossD = loss_true + loss_fake

    scaler.scale(lossD).backward()
    scaler.step(opt_D)
    
    scaler.update()
    batch_time = time.time() - start_time
    if iteration % show_step == 0:
        image = make_image(Xs, Xt, Y)
        writer.add_image('Train/Xs Xt Y', image[::-1, :, :], niter)
        writer.add_scalars('Train/Generator losses',
                {'L_adv': L_adv.item(), 'L_id': L_id.item(),
                    'L_attr': L_attr.item(), 'L_rec': L_rec.item()},
                niter)
        writer.add_scalars('Train/Adversarial losses',
                {'Generator': lossG.item(), 'Discriminator': lossD.item()},
                niter)
    print(f'niter: {niter} (epoch: {epoch} {iteration}/{len(train_dataloader)})')
    print(f'    lossD: {lossD.item()} lossG: {lossG.item()} batch_time: {batch_time}s')
    print(f'    L_adv: {L_adv.item()} L_id: {L_id.item()} L_attr: {L_attr.item()} L_rec: {L_rec.item()}')
    if iteration % 1000 == 0:
        torch.save(G.state_dict(), './saved_models/G_latest.pth')
        torch.save(D.state_dict(), './saved_models/D_latest.pth')
        torch.save(opt_D.state_dict(), './saved_models/optG_latest.pth')
        torch.save(opt_D.state_dict(), './saved_models/optD_latest.pth')
        torch.save(scaler.state_dict(), './saved_models/scaler_latest.pth')
        with open('./saved_models/niter.pkl', 'wb') as f:
            pickle.dump(niter, f)
    if (niter + 1) % 10000 == 0:
        torch.save(G.state_dict(), f'./saved_models/G_iteration_{niter + 1}.pth')
        torch.save(D.state_dict(), f'./saved_models/D_iteration_{niter + 1}.pth')
        with open(f'./saved_models/niter_{niter + 1}.pkl', 'wb') as f:
            pickle.dump(niter, f)
